"""Live search against the real engines.

The extractors in ``webskrap.search`` are written against saved fixtures and
rot when an engine changes its markup. The unit tests cannot catch that, so
this one loads real results pages. It is opt-in:

    WEBSKRAP_LIVE=1 pytest tests/test_search_live.py

An engine that distrusts the exit address serves a bot challenge instead of
results (DuckDuckGo does so from many cloud ranges). That is reported as
``blocked`` and skips here, because it says something about the address, not
the parser. Only a page that loads and yields nothing counts as rot.
"""

from __future__ import annotations

import os

import pytest

from webskrap import SearchEngine, SessionConfig, WebSkrapClient, WebSkrapError
from webskrap.errors import ErrorCode

pytestmark = [pytest.mark.browser, pytest.mark.live]

LIVE_CONFIG = SessionConfig(
    driver="patchright",
    channel=os.environ.get("WEBSKRAP_BROWSER_CHANNEL", "chrome"),
    headless=True,
    navigation_timeout_ms=60_000,
    decline_cookies=True,
)


@pytest.fixture(autouse=True)
def _require_live() -> None:
    if not os.environ.get("WEBSKRAP_LIVE"):
        pytest.skip("set WEBSKRAP_LIVE=1 to run live search tests")


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", list(SearchEngine))
async def test_engine_markup_still_parses(engine: SearchEngine) -> None:
    try:
        async with WebSkrapClient() as client:
            result = await client.search(
                "example domain", engine=engine, max_results=5, config=LIVE_CONFIG
            )
    except WebSkrapError as exc:
        if exc.code is ErrorCode.BLOCKED:
            pytest.skip(f"{engine.value} served a bot challenge to this exit IP")
        if exc.code is ErrorCode.BROWSER_LAUNCH:
            pytest.skip(f"Playwright browser unavailable: {exc}")
        raise

    assert result.ok, f"{engine.value} answered {result.status}"
    # A results page with no extractable hits is what selector rot looks like.
    assert result.hits, f"{engine.value} page loaded but no hits were parsed"
    assert len(result.hits) <= 5
    assert result.hits_total >= len(result.hits)
    for hit in result.hits:
        assert hit.title
        assert hit.url.startswith(("http://", "https://"))
        assert "duckduckgo.com/l/" not in hit.url
        assert "bing.com/ck/" not in hit.url
