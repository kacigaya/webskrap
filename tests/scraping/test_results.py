import pytest
from pydantic import ValidationError

from webskrap.config.search import SearchEngine
from webskrap.scraping.results import (
    FetchResult,
    SearchHit,
    SearchResult,
    shape_fetch_result,
    shape_search_result,
)


def test_shape_fetch_result_pages_through_a_long_body() -> None:
    result = FetchResult(
        url="https://example.test",
        final_url="https://example.test",
        status=200,
        ok=True,
        headers={},
        text="0123456789",
        title="t",
        cookies=[],
        timings={"elapsed_ms": 1.0},
    )

    first = shape_fetch_result(result, 4)
    assert first["text"] == "0123"
    assert first["text_offset"] == 0
    assert first["text_truncated"] is True
    assert first["next_text_offset"] == 4

    second = shape_fetch_result(result, 4, first["next_text_offset"])
    assert second["text"] == "4567"
    assert second["next_text_offset"] == 8

    last = shape_fetch_result(result, 4, second["next_text_offset"])
    assert last["text"] == "89"
    assert last["text_truncated"] is False
    assert last["next_text_offset"] is None


def _search_result(hits: list[SearchHit], total: int) -> SearchResult:
    return SearchResult(
        query="example domain",
        engine=SearchEngine.DDG,
        url="https://html.duckduckgo.com/html/?q=example+domain",
        final_url="https://html.duckduckgo.com/html/?q=example+domain",
        status=200,
        ok=True,
        hits=hits,
        hits_total=total,
        timings={"elapsed_ms": 12.34},
    )


def test_search_hit_snippet_is_optional() -> None:
    hit = SearchHit(title="Example Domain", url="https://example.com/")

    assert hit.snippet == ""


def test_shape_search_result_reports_what_the_cap_hid() -> None:
    hits = [SearchHit(title="Example Domain", url="https://example.com/", snippet="Illustrative.")]

    payload = shape_search_result(_search_result(hits, 3))

    assert payload == {
        "query": "example domain",
        "engine": "ddg",
        "url": "https://html.duckduckgo.com/html/?q=example+domain",
        "final_url": "https://html.duckduckgo.com/html/?q=example+domain",
        "status": 200,
        "ok": True,
        "hits": [
            {"title": "Example Domain", "url": "https://example.com/", "snippet": "Illustrative."}
        ],
        "hits_total": 3,
        "hits_truncated": True,
        "elapsed_ms": 12.3,
        "cookie_notice_declined": None,
    }


def test_shape_search_result_is_not_truncated_when_every_hit_is_returned() -> None:
    payload = shape_search_result(_search_result([], 0))

    assert payload["hits"] == []
    assert payload["hits_truncated"] is False


def test_search_result_rejects_an_unknown_engine() -> None:
    with pytest.raises(ValidationError):
        SearchResult(
            query="q",
            engine="google",  # type: ignore[arg-type]
            url="https://example.test",
            final_url="https://example.test",
            status=200,
            ok=True,
        )


def test_shape_fetch_result_filters_response_headers() -> None:
    result = FetchResult(
        url="https://example.test",
        final_url="https://example.test",
        status=200,
        ok=True,
        headers={
            "content-type": "text/html",
            "set-cookie": "session=abc",
            "Set-Cookie": "other=1",
            "authorization": "Bearer x",
            "x-request-id": "42",
        },
        text="body",
        title="t",
        cookies=[],
        timings={"elapsed_ms": 1.0},
    )

    payload = shape_fetch_result(result, 100)

    assert payload["headers"] == {"content-type": "text/html", "x-request-id": "42"}
