from __future__ import annotations

import asyncio

import pytest

from webskrap import browser_session, mcp_server
from webskrap.client import _collect_links
from webskrap.models import FetchResult, text_window


def test_mcp_response_defaults_and_text_annotations() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
    for name in ("fetch", "stealth_fetch", "browser_snapshot", "browser_text", "browser_eval"):
        assert tools[name].input_schema["properties"]["max_chars"]["default"] == 8000
    assert tools["browser_snapshot"].input_schema["properties"]["depth"]["default"] == 6
    assert tools["browser_text"].annotations.read_only_hint is True
    assert tools["browser_text"].annotations.destructive_hint is False


async def test_browser_text_routes_through_session(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    page = object()

    async def read_text(actual_page, **kwargs):
        assert actual_page is page
        captured.update(kwargs)
        return {"text": "window"}

    async def action(session, callback, timeout_ms):
        assert session == "reading"
        assert timeout_ms == browser_session.DEFAULT_ACTION_TIMEOUT_MS
        return await callback(page)

    monkeypatch.setattr(browser_session, "read_text", read_text)
    monkeypatch.setattr(mcp_server, "_browser_action", action)
    assert await mcp_server.browser_text("reading", max_chars=42, offset=9) == {"text": "window"}
    assert captured == {"max_chars": 42, "offset": 9}


@pytest.fixture
async def page(sandbox_supported: bool):
    from patchright.async_api import async_playwright

    async with async_playwright() as driver:
        browser = await driver.chromium.launch(chromium_sandbox=sandbox_supported)
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


@pytest.mark.browser
@pytest.mark.parametrize("limit,offset", [(8000, 0), (4, 2), (0, 3), (-2, -3), (8, 100)])
async def test_visible_text_window_matches_python(page, limit: int, offset: int) -> None:
    await page.set_content(
        "<title>Reading</title><body><div>Visible 😀 text 𐐀</div>"
        '<span style="display:none">Hidden text</span><script>/* script text */</script></body>'
    )
    full = await page.locator("body").inner_text()
    expected = text_window(full, limit, offset)
    result = await browser_session.read_text(page, max_chars=limit, offset=offset)
    assert result == {
        "url": page.url,
        "title": "Reading",
        "text": expected.text,
        "text_length": expected.length,
        "text_offset": expected.offset,
        "text_truncated": expected.truncated,
        "next_text_offset": expected.next_offset,
    }


@pytest.mark.browser
async def test_text_pages_reassemble_large_unicode_body(page) -> None:
    await page.set_content("<body></body>")
    await page.locator("body").evaluate("body => { body.textContent = 'abc😀'.repeat(6000); }")
    full = await page.locator("body").inner_text()
    chunks = []
    offset = 0
    while True:
        result = await browser_session.read_text(page, max_chars=8000, offset=offset)
        assert len(result["text"]) <= 8000
        assert result["text_length"] == len(full)
        chunks.append(result["text"])
        offset = result["next_text_offset"]
        if offset is None:
            break
    assert "".join(chunks) == full
    assert len(chunks) == 3


@pytest.mark.browser
@pytest.mark.parametrize("limit", [0, 2, -1])
async def test_link_cap_skips_unused_label_extraction(page, limit: int) -> None:
    await page.set_content("<body></body>")
    await page.evaluate(
        """() => {
      for (let index = 0; index < 100; index++) {
        const anchor = document.createElement('a');
        anchor.href = `https://example.test/${index % 50}`;
        document.body.append(anchor);
      }
    }""",
        isolated_context=False,
    )
    # Install the counters in the same isolated world used by link collection.
    await page.evaluate("""() => {
      globalThis.labelReads = 0;
      for (const anchor of document.querySelectorAll('a')) {
        Object.defineProperty(anchor, 'innerText', {get() {
          globalThis.labelReads++;
          return 'Visible label';
        }});
      }
    }""")
    links, total = await _collect_links(page, limit)
    assert total == 50
    assert len(links) == max(0, limit)
    assert await page.evaluate("globalThis.labelReads") == max(0, limit)


@pytest.mark.browser
async def test_browser_text_reads_persistent_session(persistent_session_env) -> None:
    await mcp_server.browser_open(session="text-reading")
    try:

        async def prepare(page):
            await page.set_content("<title>Session</title><body>abcdefghij</body>")

        await browser_session.run_page_action("text-reading", prepare)
        result = await mcp_server.browser_text("text-reading", max_chars=4, offset=2)
        assert result["title"] == "Session"
        assert result["text"] == "cdef"
        assert result["text_length"] == 10
        assert result["next_text_offset"] == 6
    finally:
        await mcp_server.browser_close(session="text-reading")


@pytest.mark.browser
async def test_shallow_snapshot_can_expand_to_full_tree(page) -> None:
    html = "<button>Deep action</button>"
    for depth in range(10):
        html = f'<div role="group" aria-label="Level {depth}">{html}</div>'
    await page.set_content(html)
    shallow = await browser_session.snapshot(page, depth=6)
    full = await browser_session.snapshot(page, depth=None)
    assert len(shallow["snapshot"]) < len(full["snapshot"])
    assert "Deep action" not in shallow["snapshot"]
    assert "Deep action" in full["snapshot"]


@pytest.mark.parametrize("tool", [mcp_server.fetch, mcp_server.stealth_fetch])
async def test_fetch_default_cap_and_explicit_override(monkeypatch, tool) -> None:
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def fetch(self, url, **_kwargs):
            return FetchResult(
                url=url,
                final_url=url,
                status=200,
                ok=True,
                headers={},
                text="x" * 24000,
                title="Large page",
                cookies=[],
                timings={},
            )

    monkeypatch.setattr(mcp_server, "WebSkrapClient", Client)
    result = await tool("https://example.test")
    assert len(result["text"]) == 8000
    assert result["text_length"] == 24000
    assert result["text_truncated"] is True
    assert result["next_text_offset"] == 8000
    result = await tool("https://example.test", max_chars=24000)
    assert len(result["text"]) == 24000
    assert result["text_truncated"] is False
