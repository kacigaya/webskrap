from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import tls_context

from webskrap import SessionConfig, WebSkrapClient, challenges, datadome
from webskrap.cdp import CDPPage
from webskrap.cdp_dom import Frame
from webskrap.datadome import Slider, box, drag_path, locate
from webskrap.errors import ErrorCode, WebSkrapError

CAPTCHA = {"provider": "DataDome", "kind": "challenge", "reloads": False}
HANDLE = {"x": 60.0, "y": 300.0, "width": 40.0, "height": 40.0}
SLOT = {"x": 260.0, "y": 300.0, "width": 40.0, "height": 40.0}


def test_box_rejects_a_hidden_element() -> None:
    assert box([10, 20, 50, 20, 50, 60, 10, 60]) == {"x": 10, "y": 20, "width": 40, "height": 40}
    assert box([10, 20, 12, 20, 12, 60, 10, 60]) is None
    assert box([0, 0, 0, 0, 0, 0, 0, 0]) is None


def test_drag_path_eases_onto_the_slot() -> None:
    path = drag_path((65.0, 320.0), 280.0)
    assert len(path) == datadome.DRAG_STEPS
    assert path[-1][0] == 280
    assert all(abs(y - 320) <= 2 for _, y in path)
    assert all(65 <= x <= 284 for x, _ in path)
    assert path[0][0] < path[len(path) // 2][0] < path[-1][0]


def _frame(page: CDPPage, session: str, url: str, owner: bool = True) -> Frame:
    frame_page = MagicMock(spec=CDPPage)
    frame_page.session_id = session
    frame_page.send = AsyncMock(side_effect=AssertionError("measured the wrong frame"))
    return Frame(frame_page, page, url, 40.0, 74.0, ("S0", 9) if owner else None)


def _widget_send(nodes: dict[str, list[float]]) -> Any:
    async def send(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.querySelector":
            selector = params["selector"]
            return {"nodeId": list(nodes).index(selector) + 2 if selector in nodes else 0}
        return {"model": {"content": list(nodes.values())[params["nodeId"] - 2]}}

    return send


async def test_locate_measures_only_the_cross_site_captcha_frame(monkeypatch) -> None:
    page = MagicMock(spec=CDPPage)
    page.session_id = "S0"
    frames = [
        _frame(page, "S0", "https://example.test/", owner=False),
        _frame(page, "S0", "https://geo.captcha-delivery.com/captcha/"),
        _frame(page, "S1", "https://ads.example.test/captcha/"),
        _frame(page, "S2", "https://geo.captcha-delivery.com/interstitial/"),
    ]
    widget = _frame(page, "S3", "https://geo.captcha-delivery.com/captcha/?initialCid=x")
    widget.page.send = AsyncMock(
        side_effect=_widget_send(
            {
                ".slider": [20, 226, 60, 226, 60, 266, 20, 266],
                ".sliderTarget": [220, 226, 260, 226, 260, 266, 220, 266],
            }
        )
    )
    monkeypatch.setattr(datadome.cdp_dom, "frames", AsyncMock(return_value=[*frames, widget]))
    assert await locate(page) == (HANDLE, SLOT)


async def test_locate_waits_until_handle_and_slot_are_drawn(monkeypatch) -> None:
    page = MagicMock(spec=CDPPage)
    page.session_id = "S0"
    widget = _frame(page, "S3", "https://geo.captcha-delivery.com/captcha/")
    widget.page.send = AsyncMock(
        side_effect=_widget_send({".slider": [20, 226, 60, 226, 60, 266, 20, 266]})
    )
    monkeypatch.setattr(datadome.cdp_dom, "frames", AsyncMock(return_value=[widget]))
    assert await locate(page) is None
    widget.page.send = AsyncMock(side_effect=WebSkrapError("frame detached"))
    assert await locate(page) is None
    monkeypatch.setattr(
        datadome.cdp_dom, "frames", AsyncMock(side_effect=WebSkrapError("navigated"))
    )
    assert await locate(page) is None


async def test_slide_presses_drags_and_releases_on_the_slot(monkeypatch) -> None:
    monkeypatch.setattr(datadome, "uniform", lambda low, _high: 0.0)
    travel = AsyncMock()
    monkeypatch.setattr(datadome.cdp_dom, "travel", travel)
    page = MagicMock(spec=CDPPage)
    page.send = AsyncMock(return_value={})
    await datadome._slide(page, HANDLE, SLOT)
    assert travel.await_args_list[-1].args == (page, 65.0, 320.0)
    events = [call.args[1] for call in page.send.await_args_list]
    assert [event["type"] for event in events] == [
        "mousePressed",
        *["mouseMoved"] * datadome.DRAG_STEPS,
        "mouseReleased",
    ]
    assert (events[0]["x"], events[0]["y"]) == (65.0, 320.0)
    assert all(event["buttons"] == 1 for event in events[:-1])
    assert (events[-1]["x"], events[-1]["y"]) == (280, 320)
    assert page.pointer == (280, 320)


@pytest.fixture
def gesture(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    mocks = {
        "locate": AsyncMock(return_value=(HANDLE, SLOT)),
        "wander": AsyncMock(),
        "_slide": AsyncMock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(datadome, name, mock)
    monkeypatch.setattr(datadome.cdp_dom, "frames", AsyncMock(return_value=[]))
    return mocks


async def test_step_wanders_once_then_slides_on_schedule(gesture) -> None:
    slider = Slider(MagicMock(spec=CDPPage))
    await slider.step()
    assert slider.seen and slider.slides == 1
    gesture["wander"].assert_awaited_once()
    gesture["_slide"].assert_awaited_once_with(slider.page, HANDLE, SLOT)
    assert slider.next_slide > time.monotonic() + datadome.SLIDE_INTERVAL_S - 1
    await slider.step()
    assert slider.slides == 1
    for _ in range(5):
        slider.next_slide = 0.0
        await slider.step()
    assert slider.slides == datadome.MAX_SLIDES
    assert gesture["wander"].await_count == 1


async def test_step_does_nothing_without_a_slider_or_past_the_deadline(gesture) -> None:
    gesture["locate"].return_value = None
    slider = Slider(MagicMock(spec=CDPPage))
    await slider.step()
    assert not slider.seen and slider.slides == 0
    gesture["locate"].return_value = (HANDLE, SLOT)
    slider.deadline = time.monotonic() - 1
    await slider.step()
    assert slider.slides == 0
    gesture["_slide"].assert_not_awaited()


async def test_a_slider_gone_after_wandering_is_not_dragged(gesture) -> None:
    gesture["locate"].side_effect = [(HANDLE, SLOT), None]
    slider = Slider(MagicMock(spec=CDPPage))
    await slider.step()
    assert slider.seen and slider.slides == 0
    gesture["_slide"].assert_not_awaited()


async def test_a_gesture_cut_short_by_navigation_still_counts(gesture) -> None:
    gesture["_slide"].side_effect = WebSkrapError("page navigated")
    slider = Slider(MagicMock(spec=CDPPage))
    await slider.step()
    assert slider.slides == 1


def _captcha_page(responses: list[Any]) -> tuple[MagicMock, MagicMock]:
    page = MagicMock(spec=CDPPage)
    page.target_id = "F1"
    page.default_timeout_s = 5.0
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


async def test_settle_slides_then_returns_the_page_the_captcha_reloads_into(gesture) -> None:
    real = {"status": 200, "headers": {}}
    page, events = _captcha_page([CAPTCHA, CAPTCHA, None])

    async def slide(*_: Any) -> None:
        events.events += [
            ("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": "L2"}}),
            ("Network.responseReceived", {"loaderId": "L2", "type": "Document", "response": real}),
        ]

    gesture["_slide"].side_effect = slide
    settled = await challenges.settle(
        page, events, {}, wait_until="load", deadline=time.monotonic() + 5, solve_datadome=True
    )
    assert settled is real
    gesture["_slide"].assert_awaited_once()
    page.wait_lifecycle.assert_awaited_once()


async def test_settle_stops_after_the_last_slide_and_its_budget(gesture, monkeypatch) -> None:
    monkeypatch.setattr(challenges, "_POLL_S", 0.01)
    monkeypatch.setattr(datadome, "TIMEOUT_S", 0.4)
    monkeypatch.setattr(datadome, "SLIDE_INTERVAL_S", 0.05)
    page, events = _captcha_page([CAPTCHA] * 1000)
    started = time.monotonic()
    settled = await challenges.settle(
        page, events, {}, wait_until="load", deadline=time.monotonic() + 0.05, solve_datadome=True
    )
    assert settled == {"status": 403, "headers": {}}
    assert gesture["_slide"].await_count == datadome.MAX_SLIDES
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("flags", [{}, {"solve_cloudflare": True}])
async def test_settle_leaves_datadome_alone_without_the_flag(gesture, flags) -> None:
    page, events = _captcha_page([CAPTCHA])
    settled = await challenges.settle(
        page, events, {}, wait_until="load", deadline=time.monotonic() + 30, **flags
    )
    assert settled == {"status": 403, "headers": {}}
    gesture["locate"].assert_not_awaited()


def test_datadome_challenge_hints_name_the_flag() -> None:
    challenge = challenges.Detection("DataDome", "challenge")
    assert challenge.hint().endswith("solve_datadome=True drags its slider.")
    assert "solve_datadome" in challenge.hint(browser_session=True)
    assert "solve_datadome" not in challenges.Detection("DataDome", "block").hint()
    assert "solve_datadome" not in challenges.Detection("Cloudflare", "challenge").hint()


CAPTCHA_PAGE = """<html><head><title>Protected page</title></head><body>
<iframe src="https://geo.captcha-delivery.com:{port}/captcha/?mode={mode}"
  width="400" height="200" style="border:0"></iframe>
<script>addEventListener('message', (e) => {{
  if (e.data === 'cleared') {{ document.cookie = 'cleared=1; path=/'; location.reload(); }}
}});</script></body></html>"""

WIDGET = b"""<html><body style="margin:0;position:relative;width:400px;height:200px">
<div class="sliderTarget" style="position:absolute;left:300px;top:80px;width:40px;height:40px;
  background:#ccc"></div>
<div class="slider" style="display:none;position:absolute;left:20px;top:80px;width:40px;
  height:40px;background:#333"></div>
<script>
const mode = new URLSearchParams(location.search).get('mode');
const handle = document.querySelector('.slider');
setTimeout(() => { handle.style.display = 'block'; }, 700);
let grab = null, moves = 0;
handle.addEventListener('mousedown', (e) => { grab = e.clientX - handle.offsetLeft; moves = 0; });
addEventListener('mousemove', (e) => {
  if (grab === null || !(e.buttons & 1)) return;
  moves += 1;
  handle.style.left = (e.clientX - grab) + 'px';
});
addEventListener('mouseup', (e) => {
  if (grab === null) return;
  grab = null;
  const query = new URLSearchParams({x: e.clientX, moves, trusted: e.isTrusted});
  fetch('/dragged?' + query).then(() => {
    if (mode === 'pass') parent.postMessage('cleared', '*');
    else handle.style.left = '20px';
  });
});
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/dragged":
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            self.server.drags.append(query)  # type: ignore[attr-defined]
            self._send(204, b"")
        elif url.path == "/captcha/":
            self._send(200, WIDGET)
        elif "cleared=1" in self.headers.get("Cookie", ""):
            self._send(200, b"<html><title>Real page</title><body>Welcome back</body></html>")
        else:
            port = self.server.widget_port  # type: ignore[attr-defined]
            body = CAPTCHA_PAGE.format(port=port, mode=url.path.strip("/"))
            self._send(403, body.encode())

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def captcha_server(tmp_path: Path) -> Iterator[ThreadingHTTPServer]:
    page, widget = (ThreadingHTTPServer(("127.0.0.1", 0), _Handler) for _ in range(2))
    context = tls_context(tmp_path, datadome.FRAME_HOST)
    widget.socket = context.wrap_socket(widget.socket, server_side=True)
    page.drags = widget.drags = []  # type: ignore[attr-defined]
    page.widget_port = widget.server_port  # type: ignore[attr-defined]
    for server in (page, widget):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    yield page
    for server in (page, widget):
        server.shutdown()
        server.server_close()


def _config(sandbox_supported: bool) -> SessionConfig:
    return SessionConfig(
        channel="chromium",
        chromium_sandbox=sandbox_supported,
        ignore_https_errors=True,
        solve_datadome=True,
        launch_args=["--host-resolver-rules=MAP geo.captcha-delivery.com 127.0.0.1"],
    )


@pytest.mark.browser
async def test_fetch_drags_the_slider_onto_the_slot_and_returns_the_real_page(
    captcha_server, sandbox_supported
) -> None:
    url = f"http://127.0.0.1:{captcha_server.server_port}/pass"
    async with WebSkrapClient(default_config=_config(sandbox_supported)) as client:
        result = await client.fetch(url, text_only=True)
    assert (result.status, result.title, result.text) == (200, "Real page", "Welcome back")
    [drag] = captcha_server.drags
    assert drag["trusted"] == "true"
    assert int(drag["moves"]) >= datadome.DRAG_STEPS - 1
    assert 317 <= float(drag["x"]) <= 323


@pytest.mark.browser
async def test_fetch_reports_a_slider_that_never_clears(
    captcha_server, sandbox_supported, monkeypatch
) -> None:
    monkeypatch.setattr(challenges, "INTERSTITIAL_TIMEOUT_S", 5.0)
    monkeypatch.setattr(datadome, "TIMEOUT_S", 30.0)
    monkeypatch.setattr(datadome, "SLIDE_INTERVAL_S", 0.5)
    monkeypatch.setattr(datadome, "WANDER_S", (0.0, 0.1))
    monkeypatch.setattr(datadome, "DRAG_STEPS", 12)
    url = f"http://127.0.0.1:{captcha_server.server_port}/stay"
    started = time.monotonic()
    async with WebSkrapClient(default_config=_config(sandbox_supported)) as client:
        with pytest.raises(WebSkrapError) as error:
            await client.fetch(url)
    assert error.value.code is ErrorCode.BLOCKED
    assert len(captcha_server.drags) == datadome.MAX_SLIDES
    assert time.monotonic() - started < 60
