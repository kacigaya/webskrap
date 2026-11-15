"""Parse HTML into a tree and query it with a small CSS selector subset.

Works offline on any HTML string, including ``FetchResult.text`` from a fetch
without ``text_only``. It is built on :mod:`html.parser` and adds no
dependency.

The parser handles void elements, self-closing tags, stray end tags and the
common implied end tags (``p``, ``li``, ``option``, ``dt``/``dd``, table rows
and cells). It does not run the full HTML5 tree-construction algorithm, so
badly broken markup can nest differently than in a browser. When the exact
browser tree matters, query the live page from a ``WebSkrapSession`` instead.

Selectors support type and ``*``, ``#id``, ``.class``, attribute tests
(``[a]``, ``=``, ``~=``, ``|=``, ``^=``, ``$=``, ``*=``), the descendant and
``>`` combinators, and ``,`` lists. Other syntax, such as pseudo-classes and
sibling combinators, raises ``ValueError`` instead of matching wrongly.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from html.parser import HTMLParser

_VOID = frozenset(
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
        "param",
        "source",
        "track",
        "wbr",
    }
)
_IMPLIED_END = {
    "li": frozenset({"li"}),
    "option": frozenset({"option"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "tr": frozenset({"tr", "td", "th"}),
    "td": frozenset({"td", "th"}),
    "th": frozenset({"td", "th"}),
}
_CLOSES_P = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "details",
        "div",
        "dl",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "main",
        "menu",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "ul",
    }
)
_TOKEN = re.compile(
    r"""
    \s*(?P<comb>[>,])\s*
    | (?P<space>\s+)
    | (?P<type>\*|[-\w]+)
    | \#(?P<id>[-\w]+)
    | \.(?P<cls>[-\w]+)
    | \[\s*(?P<attr>[-\w:]+)\s*
      (?:(?P<op>[~|^$*]?=)\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[-\w]+))\s*)?
      \]
    """,
    re.VERBOSE,
)
_ATTR_OPS: dict[str, Callable[[str, str], bool]] = {
    "": lambda actual, wanted: True,
    "=": lambda actual, wanted: actual == wanted,
    "~=": lambda actual, wanted: wanted in actual.split(),
    "|=": lambda actual, wanted: actual == wanted or actual.startswith(wanted + "-"),
    "^=": lambda actual, wanted: bool(wanted) and actual.startswith(wanted),
    "$=": lambda actual, wanted: bool(wanted) and actual.endswith(wanted),
    "*=": lambda actual, wanted: bool(wanted) and wanted in actual,
}


class Element:
    """One element of a parsed document, or the document root.

    ``tag`` is lower case, and the root's tag is ``"#document"``. ``attrs``
    maps lower-case attribute names to values: ``""`` for a valueless
    attribute, and the first value when a name repeats. ``children`` holds
    child elements and text strings in document order.
    """

    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(self, tag: str, attrs: dict[str, str], parent: Element | None) -> None:
        """Create an element with no children under ``parent``."""
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Element | str] = []

    def __repr__(self) -> str:
        """Show the tag and attributes."""
        return f"<Element {self.tag} {self.attrs!r}>"

    @property
    def text(self) -> str:
        """All descendant text in document order, without whitespace changes."""
        parts: list[str] = []
        stack: list[Element | str] = [self]
        while stack:
            node = stack.pop()
            if isinstance(node, str):
                parts.append(node)
            else:
                stack.extend(reversed(node.children))
        return "".join(parts)

    def iter(self) -> Iterator[Element]:
        """Yield every descendant element in document order, not this one."""
        stack = [child for child in reversed(self.children) if isinstance(child, Element)]
        while stack:
            element = stack.pop()
            yield element
            stack.extend(
                child for child in reversed(element.children) if isinstance(child, Element)
            )

    def css(self, selector: str) -> list[Element]:
        """Return descendants that match ``selector``, in document order.

        Raises:
            ValueError: If ``selector`` is empty or uses unsupported syntax.
        """
        return list(self._select(selector))

    def css_first(self, selector: str) -> Element | None:
        """Return the first descendant that matches ``selector``, or ``None``.

        Raises:
            ValueError: If ``selector`` is empty or uses unsupported syntax.
        """
        return next(self._select(selector), None)

    def _select(self, selector: str) -> Iterator[Element]:
        matchers = [_Matcher(chain) for chain in _parse_selector(selector)]
        for element in self.iter():
            if any(matcher.matches(element) for matcher in matchers):
                yield element


@dataclass(frozen=True, slots=True)
class _Compound:
    tag: str | None
    tests: tuple[tuple[str, str, str], ...]

    def matches(self, element: Element) -> bool:
        if element.tag.startswith("#") or self.tag not in (None, element.tag):
            return False
        return all(
            name in element.attrs and _ATTR_OPS[op](element.attrs[name], value)
            for name, op, value in self.tests
        )


_Chain = list[tuple[str, _Compound]]


class _Matcher:
    """Match one selector chain, caching results per element.

    The caches make each element and chain position cost one check, so deep
    or hostile documents stay linear in size.
    """

    def __init__(self, chain: _Chain) -> None:
        self._chain = chain
        self._matched: dict[tuple[int, int], bool] = {}
        self._up: dict[tuple[int, int], bool] = {}

    def matches(self, element: Element, index: int | None = None) -> bool:
        index = len(self._chain) - 1 if index is None else index
        key = (id(element), index)
        if key not in self._matched:
            combinator, compound = self._chain[index]
            result = compound.matches(element)
            if result and index:
                if combinator == ">":
                    result = element.parent is not None and self.matches(element.parent, index - 1)
                else:
                    result = self._self_or_ancestor(element.parent, index - 1)
            self._matched[key] = result
        return self._matched[key]

    def _self_or_ancestor(self, element: Element | None, index: int) -> bool:
        seen: list[tuple[int, int]] = []
        result = False
        while element is not None:
            key = (id(element), index)
            if key in self._up:
                result = self._up[key]
                break
            seen.append(key)
            if self.matches(element, index):
                result = True
                break
            element = element.parent
        for key in seen:
            self._up[key] = result
        return result


def _parse_selector(selector: str) -> list[_Chain]:
    text = selector.strip()
    if not text:
        raise ValueError("selector must not be empty")
    chains: list[_Chain] = []
    chain: _Chain = []
    combinator = ""
    tag: str | None = None
    tests: list[tuple[str, str, str]] = []
    empty = True
    pos = 0
    while True:
        match = _TOKEN.match(text, pos) if pos < len(text) else None
        if match is None and pos < len(text):
            raise ValueError(f"unsupported selector syntax at {pos}: {text[pos : pos + 20]!r}")
        separator = None if match is None else match["comb"] or (" " if match["space"] else None)
        if match is None or separator:
            if empty:
                raise ValueError(f"selector {selector!r} has an empty part")
            chain.append((combinator, _Compound(tag, tuple(tests))))
            tag, tests, empty = None, [], True
            if match is None:
                chains.append(chain)
                return chains
            if separator == ",":
                chains.append(chain)
                chain, combinator = [], ""
            else:
                combinator = separator or " "
        else:
            if match["type"]:
                if not empty:
                    raise ValueError(f"type selector must start a part at {pos}")
                tag = None if match["type"] == "*" else match["type"].lower()
            elif match["id"]:
                tests.append(("id", "=", match["id"]))
            elif match["cls"]:
                tests.append(("class", "~=", match["cls"]))
            else:
                value = next(
                    (v for v in (match["dq"], match["sq"], match["bare"]) if v is not None), ""
                )
                tests.append((match["attr"].lower(), match["op"] or "", value))
            empty = False
        pos = match.end()


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#document", {}, None)
        self._open = [self.root]
        self._counts: Counter[str] = Counter()

    def _pop(self) -> None:
        self._counts[self._open.pop().tag] -= 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        closers = _IMPLIED_END.get(tag, frozenset())
        while len(self._open) > 1 and (
            self._open[-1].tag in closers or (tag in _CLOSES_P and self._open[-1].tag == "p")
        ):
            self._pop()
        values: dict[str, str] = {}
        for name, value in attrs:
            values.setdefault(name, value or "")
        element = Element(tag, values, self._open[-1])
        self._open[-1].children.append(element)
        if tag not in _VOID:
            self._open.append(element)
            self._counts[tag] += 1

    def handle_endtag(self, tag: str) -> None:
        if self._counts[tag] <= 0:
            return
        while self._open[-1].tag != tag:
            self._pop()
        self._pop()

    def handle_data(self, data: str) -> None:
        children = self._open[-1].children
        last = children[-1] if children else None
        if isinstance(last, str):
            children[-1] = last + data
        else:
            children.append(data)


def parse_html(html: str) -> Element:
    """Parse ``html`` and return the document root.

    The root's children are the top-level elements and text. Comments and the
    doctype are dropped, and character references are decoded.
    """
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root
