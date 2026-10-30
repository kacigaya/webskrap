from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from conftest import set_page_content

from webskrap import SessionConfig, WebSkrapClient, cdp_dom, human
from webskrap.cdp import CDPError, CDPPage
from webskrap.errors import ErrorCode, WebSkrapError


@pytest.mark.parametrize("width,height", [(1, 1), (10, 8), (200, 100)])
@pytest.mark.parametrize("edge", ["low", "high"])
def test_click_jitter_stays_inside_small_targets(monkeypatch, width, height, edge) -> None:
    monkeypatch.setattr(human, "uniform", lambda low, high: low if edge == "low" else high)
    box = human.Box(x=20, y=30, width=width, height=height)
    x, y = human.click_point(box, None)
    assert box["x"] < x < box["x"] + width
    assert box["y"] < y < box["y"] + height
    assert abs(x - (box["x"] + width / 2)) == pytest.approx(min(width * 0.2, 12))
    assert abs(y - (box["y"] + height / 2)) == pytest.approx(min(height * 0.2, 10))


@pytest.fixture
def pointer(monkeypatch):
    page = MagicMock(spec=CDPPage)
    page.send = AsyncMock(return_value={})
    element = cdp_dom._Element(page, "node", "#go")
    box = {"x": 100, "y": 100, "width": 60, "height": 40, "viewportHeight": 600}

    async def evaluate(declaration, *args):
        if declaration == cdp_dom._BOX_FUNCTION:
            return dict(box)
        if declaration == cdp_dom._STATE_FUNCTION:
            return "visible"
        if declaration in (cdp_dom._HIT_TEST_FUNCTION, cdp_dom._ENABLED_FUNCTION):
            return True
        pytest.fail(f"unexpected element call: {declaration}")

    element.call = AsyncMock(side_effect=evaluate)
    monkeypatch.setattr(cdp_dom.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(cdp_dom, "uniform", lambda low, high: (low + high) / 2)
    monkeypatch.setattr(human, "uniform", lambda low, high: (low + high) / 2)
    return element, box


@pytest.mark.parametrize("empty", [None, {"width": 0, "height": 20}, {"width": 20, "height": 0}])
async def test_pointer_recovers_a_lost_box_once(pointer, empty) -> None:
    element, box = pointer
    element.call = AsyncMock(side_effect=[empty, None, box])
    assert await cdp_dom._box(element, recover=True) == box
    assert element.call.await_args_list == [
        call(cdp_dom._BOX_FUNCTION),
        call(cdp_dom._SCROLL_FUNCTION),
        call(cdp_dom._BOX_FUNCTION),
    ]
    cdp_dom.asyncio.sleep.assert_awaited_once_with(pytest.approx(0.45))


async def test_pointer_refuses_a_box_that_stays_empty(pointer) -> None:
    element, _ = pointer
    element.call = AsyncMock(side_effect=[None, None, {"width": 0, "height": 0}])
    with pytest.raises(WebSkrapError, match="visible bounding box") as error:
        await cdp_dom._box(element, recover=True)
    assert error.value.code is ErrorCode.USAGE
    assert element.call.await_count == 3
    element.input_page.send.assert_not_awaited()


async def test_box_recovery_refreshes_frame_offsets(pointer, monkeypatch) -> None:
    element, box = pointer
    element.frame_id = "child"
    element.call = AsyncMock(side_effect=[None, box])
    scroll = AsyncMock()
    monkeypatch.setattr(cdp_dom, "_scroll_frame_element", scroll)
    assert await cdp_dom._box(element, recover=True) == box
    scroll.assert_awaited_once_with(element)


async def test_pointer_does_not_hide_protocol_errors(pointer) -> None:
    element, _ = pointer
    element.call = AsyncMock(side_effect=CDPError("context destroyed"))
    with pytest.raises(CDPError, match="context destroyed"):
        await cdp_dom._box(element, recover=True)
    assert element.call.await_count == 1


async def test_pointer_lands_on_the_exact_click_point(pointer) -> None:
    element, _ = pointer
    await cdp_dom._human_pointer(element, click_count=2)
    events = [args.args[1] for args in element.input_page.send.await_args_list]
    moves = [event for event in events if event["type"] == "mouseMoved"]
    presses = [event for event in events if event["type"] == "mousePressed"]
    releases = [event for event in events if event["type"] == "mouseReleased"]
    assert len(moves) >= 12
    assert [(press["x"], press["y"]) for press in presses] == [(moves[-1]["x"], moves[-1]["y"])] * 2
    assert [event["clickCount"] for event in presses] == [1, 2]
    assert [event["force"] for event in presses] == [cdp_dom.MOUSE_PRESSED_FORCE] * 2
    assert [event["clickCount"] for event in releases] == [1, 2]
    assert call(0.35) in cdp_dom.asyncio.sleep.await_args_list


async def test_pointer_rechecks_the_absolute_point_after_layout_moves(pointer) -> None:
    element, box = pointer
    moved = dict(box, x=300)
    element.call = AsyncMock(side_effect=[box, box, True, moved, "visible", True, False])
    with pytest.raises(WebSkrapError, match="covers the click point"):
        await cdp_dom._human_pointer(element, click_count=1)
    assert element.call.await_args_list[-1] == call(cdp_dom._HIT_TEST_FUNCTION, 130, 120)
    assert all(
        args.args[1]["type"] == "mouseMoved" for args in element.input_page.send.await_args_list
    )


async def test_hover_finishes_inside_a_small_target(pointer) -> None:
    element, box = pointer
    box.update(width=1, height=1)
    await cdp_dom._human_pointer(element)
    last = element.input_page.send.await_args.args[1]
    assert last == {"type": "mouseMoved", "x": 100.5, "y": 100.5}


async def test_consent_click_timeout_stops_before_mouse_input(pointer, monkeypatch) -> None:
    element, _ = pointer
    blocked = asyncio.Event()

    async def scroll(_element):
        await blocked.wait()

    monkeypatch.setattr(cdp_dom, "_scroll_into_view", scroll)
    with pytest.raises(WebSkrapError) as error:
        await cdp_dom.human_click(element, description="reject", timeout_s=0.01)
    assert error.value.code is ErrorCode.TIMEOUT
    element.input_page.send.assert_not_awaited()


@pytest.mark.browser
@pytest.mark.parametrize(
    "on_hover",
    [
        "this.disabled = true",
        "this.style.visibility = 'hidden'",
        "this.style.left = '400px'",
        "document.getElementById('cover').style.display = 'block'",
    ],
)
async def test_click_refuses_target_changed_by_hover(sandbox_supported, on_hover) -> None:
    html = (
        '<button id="go" style="position:absolute;left:100px;top:100px;width:100px;'
        "height:40px\" onmouseenter=\"document.body.dataset.hovered = 'yes';"
        f'{on_hover}">Go</button>'
        '<div id="cover" style="display:none;position:fixed;inset:0;z-index:10"></div>'
        "<script>document.body.dataset.downs = '0';"
        "document.addEventListener('mousedown', () => "
        "document.body.dataset.downs = String(Number(document.body.dataset.downs) + 1));</script>"
    )
    async with WebSkrapClient(
        default_config=SessionConfig(chromium_sandbox=sandbox_supported)
    ) as client:
        session = await client.session("hover-change")
        page = await session.new_page()
        await set_page_content(page, html)
        with pytest.raises(WebSkrapError) as error:
            await session.human_click(page, "#go")
        assert error.value.code is ErrorCode.USAGE
        assert await page.evaluate("document.body.dataset.hovered") == "yes"
        assert await page.evaluate("document.body.dataset.downs") == "0"
