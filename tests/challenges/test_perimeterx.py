from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

from webskrap import SessionConfig, WebSkrapClient, challenges, perimeterx
from webskrap.cdp import CDPPage
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.perimeterx import Hold, box, find_buttons, measure

CHALLENGE = {"provider": "PerimeterX", "kind": "challenge", "reloads": False}
BUTTON = {"x": 668.0, "y": 375.5, "width": 253.0, "height": 50.0}


def _node(node_id: int, name: str, *attrs: str, **extra: Any) -> dict[str, Any]:
    return {"nodeId": node_id, "nodeName": name, "attributes": list(attrs), **extra}


def _frame(frame_id: int, button_id: int) -> dict[str, Any]:
    return _node(
        frame_id,
        "IFRAME",
        contentDocument=_node(
            frame_id + 1,
            "#document",
            children=[_node(button_id, "DIV", "role", "button", "aria-label", "Press & Hold")],
        ),
    )


DOCUMENT = _node(
    1,
    "#document",
    children=[
        _node(2, "DIV", "role", "button", "id", "menu"),
        _node(
            3,
            "DIV",
            "id",
            "px-captcha-wrapper",
            children=[_node(4, "DIV", "id", "px-captcha", children=[_frame(5, 10), _frame(7, 20)])],
        ),
    ],
)


def test_finds_buttons_only_inside_the_challenge_frames() -> None:
    assert find_buttons(DOCUMENT) == [10, 20]
    assert find_buttons(_node(1, "#document", children=[_node(2, "DIV", "role", "button")])) == []


def test_box_rejects_a_hidden_element() -> None:
    assert box([1, 2, 41, 2, 41, 52, 1, 52]) == {"x": 1, "y": 2, "width": 40, "height": 50}
    assert box([0, 0, 0, 0, 0, 0, 0, 0]) is None


def _quad(item: dict[str, float]) -> list[float]:
    x, y, w, h = item["x"], item["y"], item["width"], item["height"]
    return [x, y, x + w, y, x + w, y + h, x, y + h]


def _page_send(fill: list[float]) -> Any:
    parts = {21: 251.0, 22: 253.0, 23: None}

    async def send(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "DOM.getDocument":
            assert params == {"depth": -1, "pierce": True}
            return {"root": DOCUMENT}
        if method == "DOM.querySelectorAll":
            assert params == {"nodeId": 20, "selector": "div"}
            return {"nodeIds": list(parts)}
        node_id = params["nodeId"]
        if node_id == 10:
            raise WebSkrapError("Could not compute box model.")
        if node_id == 20:
            return {"model": {"content": _quad(BUTTON)}}
        width = parts[node_id] if parts[node_id] is not None else fill[0]
        return {"model": {"content": _quad({**BUTTON, "width": width})}}

    return send


async def test_measure_counts_the_parts_that_span_the_drawn_button() -> None:
    page = MagicMock(spec=CDPPage)
    fill = [120.0]
    page.send = AsyncMock(side_effect=_page_send(fill))
    assert await measure(page) == (BUTTON, 1)
    fill[0] = 253.0
    assert await measure(page) == (BUTTON, 2)
    page.send = AsyncMock(side_effect=WebSkrapError("navigated"))
    assert await measure(page) is None


async def test_hold_presses_trembles_and_releases_when_the_fill_completes(monkeypatch) -> None:
    monkeypatch.setattr(perimeterx, "uniform", lambda low, _high: low / 100)
    monkeypatch.setattr(perimeterx.cdp_dom, "travel", AsyncMock())
    monkeypatch.setattr(
        perimeterx, "measure", AsyncMock(side_effect=[(BUTTON, 1), (BUTTON, 1), (BUTTON, 2)])
    )
    page = MagicMock(spec=CDPPage)
    page.send = AsyncMock(return_value={})
    await perimeterx._hold(page, BUTTON, 1)
    events = [call.args[1] for call in page.send.await_args_list]
    assert [event["type"] for event in events] == [
        "mousePressed",
        "mouseMoved",
        "mouseMoved",
        "mouseMoved",
        "mouseReleased",
    ]
    x, y = events[0]["x"], events[0]["y"]
    assert all(abs(e["x"] - x) <= 6 and abs(e["y"] - y) <= 4 for e in events[1:])
    assert all(event["buttons"] == 1 for event in events[:-1])
    assert (events[-1]["x"], events[-1]["y"]) == page.pointer == (events[-2]["x"], events[-2]["y"])


async def test_hold_lets_go_at_its_cap_or_when_the_page_navigates(monkeypatch) -> None:
    monkeypatch.setattr(perimeterx, "MAX_HOLD_S", 0.2)
    monkeypatch.setattr(perimeterx.cdp_dom, "travel", AsyncMock())
    monkeypatch.setattr(perimeterx, "measure", AsyncMock(return_value=(BUTTON, 1)))
    page = MagicMock(spec=CDPPage)
    page.send = AsyncMock(return_value={})
    await perimeterx._hold(page, BUTTON, 1)
    assert page.send.await_args_list[-1].args[1]["type"] == "mouseReleased"

    async def send(_method: str, params: dict[str, Any]) -> dict[str, Any]:
        if params["type"] == "mouseMoved":
            raise WebSkrapError("page navigated")
        return {}

    page.send = AsyncMock(side_effect=send)
    with pytest.raises(WebSkrapError):
        await perimeterx._hold(page, BUTTON, 1)
    assert page.send.await_args_list[-1].args[1]["type"] == "mouseReleased"


@pytest.fixture
def gesture(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    mocks = {"measure": AsyncMock(return_value=(BUTTON, 1)), "_hold": AsyncMock()}
    for name, mock in mocks.items():
        monkeypatch.setattr(perimeterx, name, mock)
    return mocks


async def test_step_holds_on_schedule_up_to_the_cap(gesture) -> None:
    hold = Hold(MagicMock(spec=CDPPage))
    await hold.step()
    assert hold.seen and hold.holds == 1
    gesture["_hold"].assert_awaited_once_with(hold.page, BUTTON, 1)
    assert hold.next_hold > time.monotonic() + perimeterx.HOLD_INTERVAL_S - 1
    await hold.step()
    assert hold.holds == 1
    for _ in range(5):
        hold.next_hold = 0.0
        await hold.step()
    assert hold.holds == perimeterx.MAX_HOLDS


async def test_step_does_nothing_without_a_button_or_past_the_deadline(gesture) -> None:
    gesture["measure"].return_value = None
    hold = Hold(MagicMock(spec=CDPPage))
    await hold.step()
    assert not hold.seen and hold.holds == 0
    gesture["measure"].return_value = (BUTTON, 1)
    hold.deadline = time.monotonic() - 1
    await hold.step()
    assert hold.holds == 0
    gesture["_hold"].assert_not_awaited()


async def test_a_gesture_cut_short_by_navigation_still_counts(gesture) -> None:
    gesture["_hold"].side_effect = WebSkrapError("page navigated")
    hold = Hold(MagicMock(spec=CDPPage))
    await hold.step()
    assert hold.holds == 1


def _challenge_page(responses: list[Any]) -> tuple[MagicMock, MagicMock]:
    page = MagicMock(spec=CDPPage)
    page.target_id = "F1"
    page.evaluate = AsyncMock(side_effect=responses)
    page.wait_lifecycle = AsyncMock()
    events = MagicMock()
    events.events = [
        ("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": "L1"}}),
        (
            "Network.responseReceived",
            {"loaderId": "L1", "type": "Document", "response": {"status": 403, "headers": {}}},
        ),
    ]
    return page, events


async def test_settle_holds_then_returns_the_page_the_challenge_reloads_into(gesture) -> None:
    real = {"status": 200, "headers": {}}
    page, events = _challenge_page([CHALLENGE, CHALLENGE, None])

    async def hold(*_: Any) -> None:
        events.events += [
            ("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": "L2"}}),
            ("Network.responseReceived", {"loaderId": "L2", "type": "Document", "response": real}),
        ]

    gesture["_hold"].side_effect = hold
    settled = await challenges.settle(
        page, events, {}, wait_until="load", deadline=time.monotonic() + 5, solve_perimeterx=True
    )
    assert settled is real
    gesture["_hold"].assert_awaited_once()
    page.wait_lifecycle.assert_awaited_once()


async def test_settle_stops_after_the_last_hold_and_its_budget(gesture, monkeypatch) -> None:
    monkeypatch.setattr(challenges, "_POLL_S", 0.01)
    monkeypatch.setattr(perimeterx, "TIMEOUT_S", 0.4)
    monkeypatch.setattr(perimeterx, "HOLD_INTERVAL_S", 0.05)
    page, events = _challenge_page([CHALLENGE] * 1000)
    started = time.monotonic()
    settled = await challenges.settle(
        page,
        events,
        {},
        wait_until="load",
        deadline=time.monotonic() + 0.05,
        solve_perimeterx=True,
    )
    assert settled == {"status": 403, "headers": {}}
    assert gesture["_hold"].await_count == perimeterx.MAX_HOLDS
    assert time.monotonic() - started < 2


async def test_settle_waits_for_the_reload_after_the_markers_vanish(gesture, monkeypatch) -> None:
    monkeypatch.setattr(challenges, "_POLL_S", 0.01)
    real = {"status": 200, "headers": {}}
    page, events = _challenge_page([])
    calls = 0

    async def detect_script(*_: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return CHALLENGE
        if calls == 6:
            events.events += [
                ("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": "L2"}}),
                (
                    "Network.responseReceived",
                    {"loaderId": "L2", "type": "Document", "response": real},
                ),
            ]
        return None

    page.evaluate = AsyncMock(side_effect=detect_script)
    settled = await challenges.settle(
        page, events, {}, wait_until="load", deadline=time.monotonic() + 5, solve_perimeterx=True
    )
    assert settled is real
    page.wait_lifecycle.assert_awaited_once()


async def test_settle_returns_after_the_grace_when_nothing_reloads(gesture, monkeypatch) -> None:
    monkeypatch.setattr(challenges, "_POLL_S", 0.01)
    monkeypatch.setattr(challenges, "RELOAD_GRACE_S", 0.2)
    page, events = _challenge_page([CHALLENGE] + [None] * 1000)
    started = time.monotonic()
    settled = await challenges.settle(
        page, events, {}, wait_until="load", deadline=time.monotonic() + 5, solve_perimeterx=True
    )
    assert settled == {"status": 403, "headers": {}}
    assert 0.2 <= time.monotonic() - started < 2


@pytest.mark.parametrize("flags", [{}, {"solve_cloudflare": True, "solve_datadome": True}])
async def test_settle_leaves_perimeterx_alone_without_the_flag(gesture, flags) -> None:
    page, events = _challenge_page([CHALLENGE])
    settled = await challenges.settle(
        page, events, {}, wait_until="load", deadline=time.monotonic() + 30, **flags
    )
    assert settled == {"status": 403, "headers": {}}
    gesture["measure"].assert_not_awaited()


def test_perimeterx_challenge_hints_name_the_flag() -> None:
    challenge = challenges.Detection("PerimeterX", "challenge")
    assert challenge.hint().endswith("solve_perimeterx=True holds its Press & Hold button.")
    assert "solve_perimeterx" in challenge.hint(browser_session=True)
    assert "solve_perimeterx" not in challenges.Detection("DataDome", "challenge").hint()


CHALLENGE_PAGE = b"""<html><head><title>Access denied</title>
<script>window._pxAppId = 'PXtest1234';</script></head><body>
<div id="px-captcha-wrapper"></div>
<script src="/test1234/captcha/captcha.js" async></script>
<script>addEventListener('message', (e) => {
  if (e.data === 'cleared') { document.cookie = 'cleared=1; path=/'; location.reload(); }
});</script></body></html>"""

CAPTCHA_JS = b"""setTimeout(() => {
  document.getElementById('px-captcha-wrapper').innerHTML = '<div id="px-captcha">'
    + '<iframe style="display:none" src="/frame?decoy=1"></iframe>'
    + '<iframe src="/frame" style="width:310px;height:52px;border:0"></iframe></div>';
}, 800);"""

FRAME = b"""<html><body style="margin:0">
<div role="button" aria-label="Press &amp; Hold" style="display:none;position:relative;
  margin-left:30px;width:253px;height:50px;background:#eee">
<div class="fill" style="position:absolute;left:0;top:0;height:50px;width:0;background:#333"></div>
<p style="position:absolute;margin:0;inset:0">Press &amp; Hold</p></div>
<script>
const mode = parent.location.pathname.slice(1);
const button = document.querySelector('[role=button]');
const fill = button.querySelector('.fill');
setTimeout(() => { button.style.display = 'block'; }, 500);
let pressed = null, moves = 0, timer = null;
button.addEventListener('mousedown', (e) => {
  pressed = {at: performance.now(), trusted: e.isTrusted}; moves = 0;
  timer = setInterval(() => {
    fill.style.width = Math.min(253, (performance.now() - pressed.at) / 2000 * 253) + 'px';
  }, 50);
});
addEventListener('mousemove', (e) => { if (pressed && (e.buttons & 1)) moves += 1; });
addEventListener('mouseup', () => {
  if (!pressed) return;
  clearInterval(timer);
  const query = new URLSearchParams({ms: Math.round(performance.now() - pressed.at),
    moves, trusted: pressed.trusted, full: fill.offsetWidth >= 253});
  pressed = null;
  fetch('/held?' + query).then(() => {
    if (mode === 'pass' && query.get('full') === 'true') parent.postMessage('cleared', '*');
    else fill.style.width = '0px';
  });
});
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, kind: str = "text/html") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/held":
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            self.server.holds.append(query)  # type: ignore[attr-defined]
            self._send(204, b"")
        elif url.path == "/frame":
            self._send(200, FRAME)
        elif url.path == "/test1234/captcha/captcha.js":
            self._send(200, CAPTCHA_JS, "text/javascript")
        elif "cleared=1" in self.headers.get("Cookie", ""):
            self._send(200, b"<html><title>Real page</title><body>Welcome back</body></html>")
        else:
            self._send(403, CHALLENGE_PAGE)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def challenge_server() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.holds = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _config(sandbox_supported: bool) -> SessionConfig:
    return SessionConfig(
        channel="chromium", chromium_sandbox=sandbox_supported, solve_perimeterx=True
    )


@pytest.mark.browser
async def test_fetch_holds_the_button_until_full_and_returns_the_real_page(
    challenge_server, sandbox_supported
) -> None:
    url = f"http://127.0.0.1:{challenge_server.server_port}/pass"
    async with WebSkrapClient(default_config=_config(sandbox_supported)) as client:
        result = await client.fetch(url, text_only=True)
    assert (result.status, result.title, result.text) == (200, "Real page", "Welcome back")
    [held] = challenge_server.holds
    assert (held["trusted"], held["full"]) == ("true", "true")
    assert 2000 <= int(held["ms"]) < 5000
    assert int(held["moves"]) >= 5


@pytest.mark.browser
async def test_fetch_reports_a_challenge_that_never_clears(
    challenge_server, sandbox_supported, monkeypatch
) -> None:
    monkeypatch.setattr(challenges, "INTERSTITIAL_TIMEOUT_S", 5.0)
    monkeypatch.setattr(perimeterx, "TIMEOUT_S", 30.0)
    monkeypatch.setattr(perimeterx, "HOLD_INTERVAL_S", 0.5)
    url = f"http://127.0.0.1:{challenge_server.server_port}/stay"
    started = time.monotonic()
    async with WebSkrapClient(default_config=_config(sandbox_supported)) as client:
        with pytest.raises(WebSkrapError) as error:
            await client.fetch(url)
    assert error.value.code is ErrorCode.BLOCKED
    assert len(challenge_server.holds) == perimeterx.MAX_HOLDS
    assert all(held["full"] == "true" for held in challenge_server.holds)
    assert time.monotonic() - started < 60
