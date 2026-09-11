"""Web search through the stealth browser: build a results URL, parse the page.

WebSkrap does not call a search API. A search loads an engine's HTML results
page in the same session a fetch uses, so proxies, consent dismissal and
persistent profiles apply unchanged, and parses the markup here in Python.
Parsing on this side rather than in page JavaScript keeps the extractors
testable against saved fixtures, and a markup change is a one-function fix in
the engine's entry of :data:`_ENGINES`.

Engines
- DuckDuckGo HTML (``ddg``): the no-JavaScript endpoint. Result links go
  through ``duckduckgo.com/l/?uddg=<url>``; the destination is unwrapped.
- Bing (``bing``): links go through ``bing.com/ck/a?u=a1<base64url>``; the
  destination is decoded. The cookie notice is declined like any other page.
- Google is deliberately absent. From a fresh headless session it answers
  with a consent wall or a CAPTCHA, and its markup changes too often to keep
  an extractor honest.

Both engines serve a challenge page from an address they distrust. That is
reported as :attr:`~webskrap.errors.ErrorCode.BLOCKED` rather than as zero
hits, so a caller does not mistake a bot check for an unanswerable query.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, urlsplit

from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.models import SearchEngine, SearchHit

__all__ = ["parse_results", "search_url"]

_WHITESPACE = re.compile(r"\s+")

# Elements that never have a closing tag. html.parser reports them as start
# tags, so without this list every <br> would swallow the rest of its parent.
_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)


@dataclass(slots=True)
class _Element:
    """One parsed element: enough of a DOM to find results by tag and class."""

    tag: str
    attrs: dict[str, str | None]
    children: list[_Element | str] = field(default_factory=list)

    @property
    def classes(self) -> frozenset[str]:
        return frozenset((self.attrs.get("class") or "").split())

    def get(self, name: str) -> str:
        return self.attrs.get(name) or ""

    def find_all(
        self,
        tag: str | None = None,
        *,
        class_: str | None = None,
        id: str | None = None,
    ) -> Iterator[_Element]:
        """Yield matching descendants in document order."""
        for child in self.children:
            if isinstance(child, str):
                continue
            if (
                (tag is None or child.tag == tag)
                and (class_ is None or class_ in child.classes)
                and (id is None or child.get("id") == id)
            ):
                yield child
            yield from child.find_all(tag, class_=class_, id=id)

    def find(
        self,
        tag: str | None = None,
        *,
        class_: str | None = None,
        id: str | None = None,
    ) -> _Element | None:
        """Return the first matching descendant, or None."""
        return next(self.find_all(tag, class_=class_, id=id), None)

    def text(self) -> str:
        """Return the element's visible text with whitespace collapsed."""
        parts: list[str] = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.text())
        return _clean(" ".join(parts))


class _TreeBuilder(HTMLParser):
    """Build an :class:`_Element` tree; tolerant of the tag soup engines emit.

    A stray end tag with no open element of that name is ignored, and an end
    tag closes everything opened after its element, which is how browsers
    treat an unclosed ``<p>`` too.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element("document", {})
        self._open = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = _Element(tag, dict(attrs))
        self._open[-1].children.append(element)
        if tag not in _VOID_ELEMENTS:
            self._open.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open[-1].children.append(_Element(tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self._open) - 1, 0, -1):
            if self._open[depth].tag == tag:
                del self._open[depth:]
                return

    def handle_data(self, data: str) -> None:
        self._open[-1].children.append(data)


def _parse_html(html: str) -> _Element:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def _clean(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


# --- DuckDuckGo ---------------------------------------------------------------


def _ddg_unwrap(href: str) -> str:
    """Return the destination behind a ``duckduckgo.com/l/?uddg=`` redirect.

    Hrefs on the HTML endpoint are scheme-relative (``//duckduckgo.com/...``).
    Anything that is not the redirect endpoint comes back as given.
    """
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if parts.netloc.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        return parse_qs(parts.query).get("uddg", [""])[0]
    return href


def _ddg_blocked(root: _Element) -> bool:
    return root.find("form", id="challenge-form") is not None


def _ddg_hits(root: _Element) -> Iterator[SearchHit]:
    for result in root.find_all("div", class_="result"):
        if "result--ad" in result.classes:
            continue
        anchor = result.find("a", class_="result__a")
        if anchor is None:
            continue
        snippet = result.find(class_="result__snippet")
        yield SearchHit(
            title=anchor.text(),
            url=_ddg_unwrap(anchor.get("href")),
            snippet=snippet.text() if snippet else "",
        )


# --- Bing -----------------------------------------------------------------------


def _bing_unwrap(href: str) -> str:
    """Return the destination behind a ``bing.com/ck/a?u=a1<base64url>`` redirect.

    The ``u`` parameter is a two-character version prefix followed by the
    URL-safe base64 of the destination, without padding. Anything that does
    not decode comes back as given rather than failing the whole page.
    """
    parts = urlsplit(href)
    if not (parts.netloc.endswith("bing.com") and parts.path == "/ck/a"):
        return href
    encoded = parse_qs(parts.query).get("u", [""])[0]
    if not encoded.startswith("a1"):
        return href
    payload = encoded[2:]
    try:
        return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return href


def _bing_blocked(root: _Element) -> bool:
    # A results page always carries the list, even when it holds only a
    # "no results" item; a challenge page does not.
    return root.find("ol", id="b_results") is None


def _bing_hits(root: _Element) -> Iterator[SearchHit]:
    for item in root.find_all("li", class_="b_algo"):
        heading = item.find("h2")
        anchor = heading.find("a") if heading else None
        if anchor is None:
            continue
        caption = item.find("p")
        yield SearchHit(
            title=anchor.text(),
            url=_bing_unwrap(anchor.get("href")),
            snippet=caption.text() if caption else "",
        )


# --- Registry -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EngineSpec:
    """Everything engine-specific: where to ask, how to read the answer."""

    endpoint: str
    hits: Callable[[_Element], Iterator[SearchHit]]
    blocked: Callable[[_Element], bool]


_ENGINES: dict[SearchEngine, _EngineSpec] = {
    SearchEngine.DDG: _EngineSpec(
        endpoint="https://html.duckduckgo.com/html/",
        hits=_ddg_hits,
        blocked=_ddg_blocked,
    ),
    SearchEngine.BING: _EngineSpec(
        endpoint="https://www.bing.com/search",
        hits=_bing_hits,
        blocked=_bing_blocked,
    ),
}


def search_url(engine: SearchEngine, query: str) -> str:
    """Return the results page URL for ``query`` on ``engine``.

    Raises:
        WebSkrapError: With code ``usage`` if the query is blank.
    """
    cleaned = _clean(query)
    if not cleaned:
        raise WebSkrapError("query must not be empty", code=ErrorCode.USAGE)
    return f"{_ENGINES[engine].endpoint}?q={quote_plus(cleaned)}"


def parse_results(engine: SearchEngine, html: str) -> list[SearchHit]:
    """Extract the organic hits from a results page.

    Ads are skipped, whitespace is collapsed, redirect wrappers are unwrapped,
    and a destination seen twice is kept once, in first-seen order. Only
    ``http`` and ``https`` destinations are returned.

    Raises:
        WebSkrapError: With code ``blocked`` if the page is a bot challenge
            rather than a results page.
    """
    spec = _ENGINES[engine]
    root = _parse_html(html)
    if spec.blocked(root):
        raise WebSkrapError(
            f"{engine.value} answered with a bot challenge instead of results",
            code=ErrorCode.BLOCKED,
        )
    seen: set[str] = set()
    hits: list[SearchHit] = []
    for hit in spec.hits(root):
        if urlsplit(hit.url).scheme not in ("http", "https") or hit.url in seen:
            continue
        seen.add(hit.url)
        hits.append(hit)
    return hits
