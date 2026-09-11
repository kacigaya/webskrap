"""Search URL building and results-page parsing against saved fixtures.

The fixtures under ``tests/fixtures/search`` are trimmed copies of what each
engine served, kept so a parser change is checked without a network. When an
engine changes its markup, refresh the fixture and fix the one extractor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.models import SearchEngine, SearchHit
from webskrap.search import _bing_unwrap, _ddg_unwrap, parse_results, search_url

FIXTURES = Path(__file__).parent / "fixtures" / "search"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        pytest.param(
            SearchEngine.DDG,
            "https://html.duckduckgo.com/html/?q=example+domain",
            id="ddg",
        ),
        pytest.param(
            SearchEngine.BING,
            "https://www.bing.com/search?q=example+domain",
            id="bing",
        ),
    ],
)
def test_search_url_encodes_the_query(engine: SearchEngine, expected: str) -> None:
    assert search_url(engine, "example domain") == expected


def test_search_url_collapses_whitespace_and_escapes_reserved_characters() -> None:
    assert search_url(SearchEngine.DDG, "  a&b   c=d\n") == (
        "https://html.duckduckgo.com/html/?q=a%26b+c%3Dd"
    )


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_search_url_rejects_a_blank_query(query: str) -> None:
    with pytest.raises(WebSkrapError) as excinfo:
        search_url(SearchEngine.DDG, query)

    assert excinfo.value.code is ErrorCode.USAGE


# --- DuckDuckGo ---------------------------------------------------------------


def test_ddg_hits_are_unwrapped_deduplicated_and_ad_free() -> None:
    hits = parse_results(SearchEngine.DDG, _fixture("ddg.html"))

    assert hits == [
        SearchHit(
            title="Example Domain",
            url="https://example.com/",
            snippet="This domain is for use in illustrative examples in documents.",
        ),
        SearchHit(
            title="Example Domains - IANA",
            url="https://www.iana.org/help/example-domains",
            snippet=(
                "As described in RFC 2606 & RFC 6761, a number of domains such as "
                "example.com are maintained for documentation purposes."
            ),
        ),
        SearchHit(
            title="Example.com - Wikipedia",
            url="https://en.wikipedia.org/wiki/Example.com",
            snippet="Example.com is a second-level domain reserved by IANA.",
        ),
        SearchHit(title="Example Org", url="https://example.org/", snippet=""),
    ]


def test_ddg_no_results_page_is_empty_not_blocked() -> None:
    assert parse_results(SearchEngine.DDG, _fixture("ddg_no_results.html")) == []


def test_ddg_challenge_page_is_reported_as_blocked() -> None:
    with pytest.raises(WebSkrapError) as excinfo:
        parse_results(SearchEngine.DDG, _fixture("ddg_challenge.html"))

    assert excinfo.value.code is ErrorCode.BLOCKED
    assert "ddg" in str(excinfo.value)


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        pytest.param(
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1%26c%3D2&rut=abc",
            "https://example.com/a?b=1&c=2",
            id="scheme-relative-redirect",
        ),
        pytest.param(
            "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F",
            "https://example.com/",
            id="absolute-redirect",
        ),
        pytest.param(
            "https://example.com/direct",
            "https://example.com/direct",
            id="direct-link",
        ),
        pytest.param(
            "//duckduckgo.com/l/?rut=abc",
            "",
            id="redirect-without-destination",
        ),
        pytest.param(
            "https://duckduckgo.com/?q=not+a+redirect",
            "https://duckduckgo.com/?q=not+a+redirect",
            id="other-duckduckgo-page",
        ),
    ],
)
def test_ddg_unwrap(href: str, expected: str) -> None:
    assert _ddg_unwrap(href) == expected


# --- Bing -----------------------------------------------------------------------


def test_bing_hits_are_decoded_deduplicated_and_ad_free() -> None:
    hits = parse_results(SearchEngine.BING, _fixture("bing.html"))

    assert hits == [
        SearchHit(
            title="Example Domain",
            url="https://example.com/",
            snippet=(
                "Jul 20, 2018 · This domain is for use in illustrative examples in documents. "
                "You may use this domain in literature without prior coordination or asking "
                "for …"
            ),
        ),
        SearchHit(
            title="Example Domains - IANA",
            url="https://www.iana.org/help/example-domains",
            snippet=(
                "As described in RFC 2606 & RFC 6761, a number of domains such as "
                "example.com are maintained for documentation purposes."
            ),
        ),
        SearchHit(
            title="Example.com - Wikipedia",
            url="https://en.wikipedia.org/wiki/Example.com",
            snippet="",
        ),
    ]


def test_bing_no_results_page_is_empty_not_blocked() -> None:
    assert parse_results(SearchEngine.BING, _fixture("bing_no_results.html")) == []


def test_bing_challenge_page_is_reported_as_blocked() -> None:
    with pytest.raises(WebSkrapError) as excinfo:
        parse_results(SearchEngine.BING, _fixture("bing_challenge.html"))

    assert excinfo.value.code is ErrorCode.BLOCKED


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        pytest.param(
            "https://www.bing.com/ck/a?!&&p=x&u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS8&ntb=1",
            "https://example.com/",
            id="unpadded-redirect",
        ),
        pytest.param(
            "https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9hP2I9MSZjPTI",
            "https://example.com/a?b=1&c=2",
            id="query-in-destination",
        ),
        pytest.param(
            "https://example.com/direct",
            "https://example.com/direct",
            id="direct-link",
        ),
        pytest.param(
            "https://www.bing.com/ck/a?u=zzaHR0cHM6Ly9leGFtcGxlLmNvbS8",
            "https://www.bing.com/ck/a?u=zzaHR0cHM6Ly9leGFtcGxlLmNvbS8",
            id="unknown-version-prefix",
        ),
        pytest.param(
            "https://www.bing.com/ck/a?u=a1%%%not-base64",
            "https://www.bing.com/ck/a?u=a1%%%not-base64",
            id="undecodable-payload",
        ),
    ],
)
def test_bing_unwrap(href: str, expected: str) -> None:
    assert _bing_unwrap(href) == expected


# --- Shared normalisation -------------------------------------------------------


def test_non_http_destinations_are_dropped() -> None:
    html = """
    <ol id="b_results">
      <li class="b_algo"><h2><a href="javascript:void(0)">Script</a></h2></li>
      <li class="b_algo"><h2><a href="mailto:someone@example.com">Mail</a></h2></li>
      <li class="b_algo"><h2><a href="http://example.com/plain">Plain</a></h2></li>
    </ol>
    """

    assert parse_results(SearchEngine.BING, html) == [
        SearchHit(title="Plain", url="http://example.com/plain")
    ]


def test_unclosed_and_void_tags_do_not_swallow_later_results() -> None:
    # A <br> with no closing tag and a <p> left open are both routine in
    # engine markup; neither may attach the next result to the previous one.
    html = """
    <div id="links" class="results">
      <div class="result"><h2><a class="result__a" href="https://a.example/">A<br>one</a></h2>
        <div class="result__snippet"><p>first
      </div></div>
      <div class="result"><h2><a class="result__a" href="https://b.example/">B</a></h2></div>
    </div>
    """

    hits = parse_results(SearchEngine.DDG, html)

    assert [hit.url for hit in hits] == ["https://a.example/", "https://b.example/"]
    assert hits[0].title == "A one"
    assert hits[0].snippet == "first"
