from __future__ import annotations

import re

import pytest
from conftest import set_page_content

from webskrap import browser_session, cdp_dom, mcp_server
from webskrap.cdp import find_executable, launch
from webskrap.errors import ErrorCode, WebSkrapError


@pytest.fixture
async def page(sandbox_supported):
    browser = await launch(
        find_executable("chromium"), args=[] if sandbox_supported else ["--no-sandbox"]
    )
    try:
        page = await browser.new_page()
        await page.enable_events()
        yield page
    finally:
        await browser.close()


@pytest.fixture
async def routed_page(page, monkeypatch):
    async def action(session, callback, timeout_ms):
        return await callback(page)

    monkeypatch.setattr(mcp_server, "_browser_action", action)
    return page


@pytest.mark.browser
async def test_scope_boxes_stability_and_stale(routed_page):
    page = routed_page
    await set_page_content(
        page,
        '<main><section aria-label="Tasks" id="tasks">'
        "<button>Save</button></section><button>Outside</button></main>",
    )
    full = await mcp_server.browser_snapshot(depth=None)
    ref = re.search(r'button "Save" \[ref=(e\d+)\]', full["snapshot"])[1]
    scoped = await mcp_server.browser_snapshot(target="#tasks", boxes=True)
    assert 'region "Tasks"' in scoped["snapshot"]
    assert "Outside" not in scoped["snapshot"]
    assert "[box=" in scoped["snapshot"]
    assert ref in scoped["snapshot"]
    assert "[box=" not in full["snapshot"]
    await page.evaluate("document.querySelector('button').textContent = 'Updated'")
    assert "Updated" in (await mcp_server.browser_snapshot(target=ref))["snapshot"]
    await page.evaluate("document.querySelector('button').remove()")
    with pytest.raises(WebSkrapError, match="stale"):
        await mcp_server.browser_snapshot(target=ref)
    with pytest.raises(WebSkrapError):
        await mcp_server.browser_snapshot(target="button, main")
    with pytest.raises(WebSkrapError, match="depth"):
        await mcp_server.browser_snapshot(depth=-1)


@pytest.mark.browser
async def test_find_semantics_limits_password_and_interact(routed_page):
    page = routed_page
    await set_page_content(
        page,
        '<main><nav aria-label="Tools"><button>Save</button>'
        "<button>Save copy</button><button>Other</button></nav>"
        '<input aria-label="Secret" type="password" value="private-secret">'
        "<button hidden>Save hidden</button>"
        '<button aria-hidden="true">Save aria hidden</button>'
        '<div style="display:none"><button>Save invisible</button></div>'
        '<button role="switch" aria-checked="mixed" aria-expanded="false"'
        ' aria-pressed="mixed" aria-busy="true">Toggle</button></main>',
    )
    found = await mcp_server.browser_find(text="SAVE", max_matches=1)
    assert found["matches_total"] == 2
    assert found["matches_truncated"] is True
    assert 'navigation "Tools"' in found["snapshot"]
    assert "Other" not in found["snapshot"]
    assert "hidden" not in found["snapshot"]
    ref = re.search(r'button "Save" \[ref=(e\d+)\]', found["snapshot"])[1]
    await browser_session.element_action(page, "click", ref, [])
    tree = (await mcp_server.browser_snapshot())["snapshot"]
    assert "private-secret" not in tree
    for state in ("checked=mixed", "expanded=false", "pressed=mixed", "busy=true"):
        assert state in tree
    found = await mcp_server.browser_find(regex='button "Save copy"')
    assert found["matches_total"] == 1
    assert "Save copy" in found["snapshot"]
    assert (await mcp_server.browser_find(text="no such control"))["snapshot"] == ""
    small = await mcp_server.browser_find(text="Save", max_chars=12)
    rest = await mcp_server.browser_find(text="Save", offset=small["next_snapshot_offset"])
    all_hits = await mcp_server.browser_find(text="Save")
    assert small["snapshot"] + rest["snapshot"] == all_hits["snapshot"]


@pytest.mark.browser
async def test_find_shadow_and_nested_controls(routed_page):
    page = routed_page
    await set_page_content(
        page,
        '<main><div id="host"></div>'
        '<div role="group" aria-label="Options"><label>Choose '
        '<input type="checkbox"></label></div></main>',
    )
    await page.evaluate("""() => {
      document.querySelector('#host').attachShadow({mode:'open'}).innerHTML =
        '<span id="label">Shadow action</span><button aria-labelledby="label"></button>';
    }""")
    found = await mcp_server.browser_find(text="Shadow action")
    assert 'button "Shadow action"' in found["snapshot"]
    ref = re.search(r'button "Shadow action" \[ref=(e\d+)\]', found["snapshot"])[1]
    assert "Shadow action" in (await mcp_server.browser_snapshot(target=ref))["snapshot"]
    assert "Choose" in (await mcp_server.browser_find(text="Choose", target="main"))["snapshot"]


@pytest.mark.browser
async def test_find_iframe_and_frame_scope(routed_page):
    page = routed_page
    await set_page_content(
        page, '<main><iframe srcdoc="&lt;button&gt;Frame action&lt;/button&gt;"></iframe></main>'
    )
    await page.evaluate("""async () => {
      const frame = document.querySelector('iframe');
      if (!frame.contentDocument?.querySelector('button'))
        await new Promise(resolve => frame.onload = resolve);
    }""")
    found = await mcp_server.browser_find(text="Frame action")
    ref = re.search(r'button "Frame action" \[ref=(f\d+e\d+)\]', found["snapshot"])[1]
    assert "iframe" in found["snapshot"]
    scoped = await mcp_server.browser_snapshot(target=ref, boxes=True)
    assert ref in scoped["snapshot"]
    await browser_session.element_action(page, "click", ref, [])


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"text": "x", "regex": "x"},
        {"regex": "("},
        {"text": ""},
        {"text": "x", "max_matches": 0},
    ],
)
async def test_find_rejects_invalid_input_before_browser(kwargs, monkeypatch):
    monkeypatch.setattr(mcp_server, "_browser_action", None)
    with pytest.raises(WebSkrapError):
        await mcp_server.browser_find(**kwargs)


async def test_context_selection_excludes_siblings_and_urls():
    tree = (
        '- main [ref=e1]:\n  - button "Save" [ref=e2]\n  - link "Other" [ref=e3]:\n    - /url: Save'
    )
    selected, total = await cdp_dom.matching_snapshot(tree, "Save", 20)
    assert total == 1
    assert selected == '- main [ref=e1]:\n  - button "Save" [ref=e2]'


@pytest.mark.browser
async def test_scope_hidden_ancestors_and_hostile_ref_text(routed_page):
    page = routed_page
    await set_page_content(
        page,
        '<div hidden><button id="hidden">Hidden action</button></div>'
        "<button>Literal [ref=#999999] and [ref=e5] text</button>"
        '<fieldset disabled><input aria-label="Disabled field" required readonly></fieldset>',
    )
    assert (await mcp_server.browser_snapshot(target="#hidden"))["snapshot"] == ""
    tree = (await mcp_server.browser_snapshot())["snapshot"]
    assert "Literal [ref=#999999] and [ref=e5] text" in tree
    assert "[disabled]" in tree
    assert "[required]" in tree
    assert "[readonly]" in tree
    assert "Literal" in (await mcp_server.browser_find(text="and [ref=e5] text"))["snapshot"]


@pytest.mark.browser
async def test_scope_depth_and_navigation_invalidate_refs(routed_page):
    page = routed_page
    await set_page_content(
        page, '<main><section aria-label="Work"><button>Start</button></section></main>'
    )
    scoped = await mcp_server.browser_snapshot(target="section", depth=1)
    assert 'region "Work"' in scoped["snapshot"]
    assert "Start" not in scoped["snapshot"]
    found = await mcp_server.browser_find(text="Start")
    ref = re.search(r'button "Start" \[ref=(e\d+)\]', found["snapshot"])[1]
    await page.goto("about:blank", wait_until="load", timeout_s=10)
    with pytest.raises(WebSkrapError, match="stale"):
        await mcp_server.browser_snapshot(target=ref)
    assert (await mcp_server.browser_find(text="Start"))["matches_total"] == 0


@pytest.mark.browser
async def test_read_only_schema_and_no_accessibility_domain(routed_page, monkeypatch):
    page = routed_page
    await set_page_content(page, "<main><button>Action</button><button>Unrelated</button></main>")
    original = page.send
    methods = []

    async def send(method, params=None):
        methods.append(method)
        return await original(method, params)

    monkeypatch.setattr(page, "send", send)
    await mcp_server.browser_find(text="Action")
    assert methods.count("DOM.describeNode") == 2
    assert not any(method.startswith("Accessibility.") for method in methods)
    assert "Runtime.enable" not in methods
    assert "Runtime.releaseObjectGroup" in methods
    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    assert tools["browser_find"].annotations.read_only_hint
    assert tools["browser_snapshot"].input_schema["properties"]["boxes"]["default"] is False


@pytest.mark.browser
async def test_modal_closed_details_and_inert(routed_page):
    page = routed_page
    await set_page_content(
        page,
        "<button>Outside</button><details><summary>More</summary>"
        "<button>Closed details</button></details><div inert><button>Inert</button></div>"
        "<dialog><button>Inside</button></dialog>",
    )
    tree = (await mcp_server.browser_snapshot())["snapshot"]
    assert "More" in tree
    assert "Closed details" not in tree
    assert "Inert" not in tree
    await page.evaluate("document.querySelector('dialog').showModal()")
    tree = (await mcp_server.browser_snapshot())["snapshot"]
    assert "dialog" in tree and "Inside" in tree
    assert "Outside" not in tree
    assert "Inside" in (await mcp_server.browser_snapshot(target="body"))["snapshot"]
    assert (await mcp_server.browser_snapshot(target="body > button"))["snapshot"] == ""


async def test_iframe_search_errors_are_not_silently_dropped(monkeypatch):
    from types import SimpleNamespace

    page = SimpleNamespace(target_id="main", session_id="main-session")
    child_page = SimpleNamespace(target_id="child", session_id="child-session")
    main = cdp_dom.Frame(page, page, "")
    child = cdp_dom.Frame(child_page, page, "", owner=("main-session", 1))

    async def frames(_page):
        return [main, child]

    async def read_frame(frame, *args):
        if frame.owner:
            raise WebSkrapError("pattern timed out", code=ErrorCode.USAGE)
        return ["- iframe [ref=e1]"], {("main-session", 1): "e1"}

    monkeypatch.setattr(cdp_dom, "frames", frames)
    monkeypatch.setattr(cdp_dom, "_frame_snapshot", read_frame)
    with pytest.raises(WebSkrapError, match="pattern timed out"):
        await cdp_dom.snapshot(page, pattern="example")
