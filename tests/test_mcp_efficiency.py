from __future__ import annotations

import asyncio

import pytest
from conftest import set_page_content

from webskrap import browser_session, mcp_server
from webskrap.cdp import find_executable, launch
from webskrap.client import LINKS_SCRIPT, WebSkrapError
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
    browser = await launch(
        find_executable("chromium"), args=[] if sandbox_supported else ["--no-sandbox"]
    )
    try:
        page = await browser.new_page()
        await page.enable_events()
        yield page
    finally:
        await browser.close()


async def _body_text(page) -> str:
    return await page.evaluate("document.body.innerText")


@pytest.mark.browser
@pytest.mark.parametrize("limit,offset", [(8000, 0), (4, 2), (0, 3), (-2, -3), (8, 100)])
async def test_visible_text_window_matches_python(page, limit: int, offset: int) -> None:
    await set_page_content(
        page,
        "<title>Reading</title><body><div>Visible 😀 text 𐐀</div>"
        '<span style="display:none">Hidden text</span><script>/* script text */</script></body>',
    )
    full = await _body_text(page)
    expected = text_window(full, limit, offset)
    result = await browser_session.read_text(page, max_chars=limit, offset=offset)
    assert result == {
        "url": "about:blank",
        "title": "Reading",
        "text": expected.text,
        "text_length": expected.length,
        "text_offset": expected.offset,
        "text_truncated": expected.truncated,
        "next_text_offset": expected.next_offset,
    }


@pytest.mark.browser
async def test_text_pages_reassemble_large_unicode_body(page) -> None:
    await set_page_content(page, "<body></body>")
    await page.evaluate("() => { document.body.textContent = 'abc😀'.repeat(6000); }")
    full = await _body_text(page)
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
    await set_page_content(page, "<body></body>")
    # Each evaluate gets a fresh isolated world, so the counters and the link
    # script run in one call.
    collected = await page.evaluate(
        f"""(max) => {{
      for (let index = 0; index < 100; index++) {{
        const anchor = document.createElement('a');
        anchor.href = `https://example.test/${{index % 50}}`;
        document.body.append(anchor);
      }}
      let labelReads = 0;
      for (const anchor of document.querySelectorAll('a')) {{
        Object.defineProperty(anchor, 'innerText', {{get() {{
          labelReads++;
          return 'Visible label';
        }}}});
      }}
      return {{...({LINKS_SCRIPT})(max), labelReads}};
    }}""",
        limit,
    )
    assert collected["total"] == 50
    assert len(collected["links"]) == max(0, limit)
    assert collected["labelReads"] == max(0, limit)


@pytest.mark.browser
async def test_browser_text_reads_persistent_session(persistent_session_env) -> None:
    await mcp_server.browser_open(session="text-reading")
    try:

        async def prepare(page):
            await set_page_content(
                page, "<title>Session</title><body>abcdefghij</body>", block_requests=False
            )

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
    await set_page_content(page, html)
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


async def test_browser_text_pattern_greps_through_session(monkeypatch: pytest.MonkeyPatch) -> None:
    page = object()
    captured = {}

    async def search_text(actual_page, pattern, **kwargs):
        assert actual_page is page
        captured.update(kwargs, pattern=pattern)
        return {"matches": []}

    async def action(session, callback, timeout_ms):
        return await callback(page)

    monkeypatch.setattr(browser_session, "search_text", search_text)
    monkeypatch.setattr(mcp_server, "_browser_action", action)
    result = await mcp_server.browser_text("reading", pattern="Price", context=2, max_matches=5)
    assert result == {"matches": []}
    assert captured == {"pattern": "Price", "context": 2, "max_matches": 5}


async def test_browser_text_rejects_a_bad_pattern_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "_browser_action", None)  # must not be reached
    with pytest.raises(WebSkrapError, match=r"invalid pattern.*\[code: usage\]"):
        await mcp_server.browser_text(pattern="(")
