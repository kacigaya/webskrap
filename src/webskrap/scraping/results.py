"""Fetch and search results and bounded text output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr

from webskrap.config.search import SearchEngine
from webskrap.driver.text_search import text_window
from webskrap.scraping.markup import Element, parse_html

#: Response headers kept in the shaped CLI/MCP fetch payload. Everything else
#: -- notably ``set-cookie``, ``cookie``, ``authorization`` and ``proxy-*`` --
#: is dropped so a model-facing payload does not carry session secrets.
SHAPED_RESPONSE_HEADERS: frozenset[str] = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "last-modified",
        "etag",
        "cache-control",
        "expires",
        "location",
        "server",
        "x-request-id",
    }
)


class Link(BaseModel):
    """One outbound anchor: its resolved absolute URL and its visible label."""

    href: str
    text: str


class FetchResult(BaseModel):
    """What one fetch produced.

    ``text`` is page HTML unless the fetch asked for ``text_only``. ``ok`` is
    an HTTP-status judgement (2xx/3xx), not a promise that the page is what you
    wanted. ``cookies`` are the whole context's, not just this page's.
    :meth:`css` and :meth:`css_first` query ``text`` as HTML.
    """

    url: str
    final_url: str
    status: int | None
    ok: bool
    headers: dict[str, str]
    text: str
    title: str
    cookies: list[dict[str, Any]]
    timings: dict[str, float]
    screenshot_path: Path | None = None
    # Strategy that dismissed a cookie consent notice ("cmp" or "text"), or
    # None when nothing was declined.
    cookie_notice_declined: str | None = None
    # Outbound links, collected only when the fetch asked for them. Empty and
    # zero otherwise, so an empty list does not distinguish "no links" from
    # "not requested" -- ``links_total`` is the count before the cap applied.
    links: list[Link] = Field(default_factory=list)
    links_total: int = 0
    _document: tuple[str, Element] | None = PrivateAttr(default=None)

    def css(self, selector: str) -> list[Element]:
        """Return elements of ``text`` that match ``selector``, in document order.

        ``text`` is parsed once with :func:`~webskrap.scraping.markup.parse_html`, and
        the tree is reused until ``text`` changes. Text from a ``text_only``
        fetch is not HTML, so query a fetch without it. See
        :mod:`webskrap.scraping.markup` for the supported selectors.

        Raises:
            ValueError: If ``selector`` is empty or uses unsupported syntax.
        """
        return self._root().css(selector)

    def css_first(self, selector: str) -> Element | None:
        """Return the first element of ``text`` that matches ``selector``, or ``None``.

        Raises:
            ValueError: If ``selector`` is empty or uses unsupported syntax.
        """
        return self._root().css_first(selector)

    def _root(self) -> Element:
        if self._document is None or self._document[0] is not self.text:
            self._document = (self.text, parse_html(self.text))
        return self._document[1]


class SearchHit(BaseModel):
    """One organic result: where it points, what it is called, and its blurb.

    ``url`` is the destination itself, with the engine's click-tracking
    redirect already unwrapped. ``snippet`` is empty when the engine showed
    none.
    """

    title: str
    url: str
    snippet: str = ""


class SearchResult(BaseModel):
    """What one search produced.

    ``url`` is the results page that was loaded; ``ok`` and ``status`` judge
    that page's HTTP response, not whether anything was found. ``hits`` is
    capped by the caller's ``max_results`` and ``hits_total`` counts what the
    page held before the cap, so a short list is distinguishable from a short
    page.
    """

    query: str
    engine: SearchEngine
    url: str
    final_url: str
    status: int | None
    ok: bool
    hits: list[SearchHit] = Field(default_factory=list)
    hits_total: int = 0
    timings: dict[str, float] = Field(default_factory=dict)
    # Strategy that dismissed a cookie consent notice on the results page, or
    # None when nothing was declined.
    cookie_notice_declined: str | None = None


def shape_fetch_result(result: FetchResult, max_chars: int, offset: int = 0) -> dict[str, Any]:
    """Flatten a result into the JSON payload the CLI and MCP tools return.

    Text is windowed to ``max_chars`` characters from ``offset``.
    ``text_length``, ``text_offset`` and ``text_truncated`` report what was cut,
    and ``next_text_offset`` is what to pass back for the rest, so a clipped
    page can be read through rather than fetched again. ``links`` is empty
    unless the fetch collected them. Cookies are left out, and headers are
    filtered to :data:`SHAPED_RESPONSE_HEADERS` so session secrets such as
    ``set-cookie`` never reach a model-facing payload.
    """
    window = text_window(result.text or "", max_chars, offset)
    shaped_headers = {
        name: value
        for name, value in result.headers.items()
        if name.lower() in SHAPED_RESPONSE_HEADERS
    }
    return {
        "url": result.url,
        "final_url": result.final_url,
        "status": result.status,
        "ok": result.ok,
        "title": result.title,
        "headers": shaped_headers,
        "text": window.text,
        "text_length": window.length,
        "text_offset": window.offset,
        "text_truncated": window.truncated,
        "next_text_offset": window.next_offset,
        "links": [link.model_dump() for link in result.links],
        "links_total": result.links_total,
        "links_truncated": result.links_total > len(result.links),
        "elapsed_ms": round(result.timings.get("elapsed_ms", 0.0), 1),
        "cookie_notice_declined": result.cookie_notice_declined,
    }


def shape_search_result(result: SearchResult) -> dict[str, Any]:
    """Flatten a search into the JSON payload the CLI and MCP tools return.

    Hits are already capped by the search itself; ``hits_truncated`` says
    whether the cap hid any, so a caller knows a larger ``max_results`` would
    return more without loading the page again to find out.
    """
    return {
        "query": result.query,
        "engine": result.engine.value,
        "url": result.url,
        "final_url": result.final_url,
        "status": result.status,
        "ok": result.ok,
        "hits": [hit.model_dump() for hit in result.hits],
        "hits_total": result.hits_total,
        "hits_truncated": result.hits_total > len(result.hits),
        "elapsed_ms": round(result.timings.get("elapsed_ms", 0.0), 1),
        "cookie_notice_declined": result.cookie_notice_declined,
    }
