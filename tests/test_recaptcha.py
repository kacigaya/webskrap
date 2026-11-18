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
from conftest import set_page_content, tls_context

from webskrap import SessionConfig, WebSkrapClient, challenges, recaptcha
from webskrap.cdp import CDPPage
from webskrap.cdp_dom import Frame
from webskrap.challenges import detect
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.recaptcha import Tick, Widget, box, locate, provider_of, solve

BOX = {"x": 21.0, "y": 31.0, "width": 28.0, "height": 28.0}
CHALLENGE = {"provider": "reCAPTCHA", "kind": "challenge", "reloads": False}


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("https://www.google.com/recaptcha/api2/anchor?k=x", "reCAPTCHA"),
        ("https://www.recaptcha.net/recaptcha/enterprise/bframe", "reCAPTCHA"),
        ("https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html", "hCaptcha"),
        ("https://www.google.com/maps/embed", None),
        ("https://www.google.com.example.test/recaptcha/api2/anchor", None),
        ("https://hcaptcha.com.example.test/captcha/v1/", None),
    ],
)
def test_provider_of_matches_only_the_widget_frames(url: str, provider: str | None) -> None:
    assert provider_of(url) == provider


def test_box_rejects_a_hidden_element() -> None:
    assert box([1, 2, 29, 2, 29, 30, 1, 30]) == {"x": 1, "y": 2, "width": 28, "height": 28}
    assert box([0, 0, 0, 0, 0, 0, 0, 0]) is None


def _frame(page: MagicMock, session: str, url: str, offset_y: float = 8.0) -> Frame:
    frame_page = MagicMock(spec=CDPPage)
    frame_page.session_id = session
    return Frame(frame_page, page, url, 8.0, offset_y, ("S0", 40 + len(session)))


def _checkbox_send(checked: bool) -> Any:
    async def send(method: str, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "DOM.getDocument": {"root": {"nodeId": 1}},
            "DOM.querySelector": {"nodeId": 2},
            "DOM.getBoxModel": {"model": {"content": [13, 23, 41, 23, 41, 51, 13, 51]}},
            "DOM.getAttributes": {
                "attributes": ["role", "checkbox", "aria-checked", str(checked).lower()]
            },
        }[method]

    return send


def _owner_send(sources: dict[int, str]) -> Any:
    async def send(method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "DOM.describeNode"
        return {"node": {"attributes": ["src", sources[params["backendNodeId"]]]}}

    return send


async def test_locate_reads_the_checkbox_and_an_on_screen_challenge(monkeypatch) -> None:
    page = MagicMock(spec=CDPPage)
    page.session_id = "S0"
    anchor = _frame(page, "S1", "https://www.google.com/recaptcha/api2/anchor?k=x")
    anchor.page.send = AsyncMock(side_effect=_checkbox_send(checked=False))
    hidden = _frame(page, "S22", "https://www.google.com/recaptcha/api2/bframe", -9999.0)
    other = _frame(page, "S333", "https://ads.example.test/frame")
    other.page.send = AsyncMock(side_effect=AssertionError("measured the wrong frame"))
    monkeypatch.setattr(
        recaptcha.cdp_dom, "frames", AsyncMock(return_value=[other, anchor, hidden])
    )
    assert await locate(page) == Widget("reCAPTCHA", BOX, False, False)
    shown = _frame(page, "S22", "https://www.google.com/recaptcha/api2/bframe", 100.0)
    monkeypatch.setattr(recaptcha.cdp_dom, "frames", AsyncMock(return_value=[anchor, shown]))
    assert (await locate(page)).challenged is True


async def test_locate_tells_hcaptcha_frames_apart_by_the_iframe_src(monkeypatch) -> None:
    page = MagicMock(spec=CDPPage)
    page.session_id = "S0"
    url = "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html"
    checkbox = _frame(page, "S1", url)
    checkbox.page.send = AsyncMock(side_effect=WebSkrapError("frame busy"))
    challenge = _frame(page, "S22", url, 120.0)
    page.send = AsyncMock(
        side_effect=_owner_send({42: f"{url}#frame=checkbox&id=a", 43: f"{url}#frame=challenge"})
    )
    monkeypatch.setattr(recaptcha.cdp_dom, "frames", AsyncMock(return_value=[checkbox]))
    assert await locate(page) == Widget("hCaptcha", None, False, False)
    monkeypatch.setattr(recaptcha.cdp_dom, "frames", AsyncMock(return_value=[checkbox, challenge]))
    assert (await locate(page)).challenged is True
    monkeypatch.setattr(recaptcha.cdp_dom, "frames", AsyncMock(side_effect=WebSkrapError("x")))
    assert await locate(page) is None


@pytest.fixture
def gesture(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    mocks = {
        "locate": AsyncMock(return_value=Widget("reCAPTCHA", BOX, False, False)),
        "wander": AsyncMock(),
        "click": AsyncMock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(recaptcha, name, mock)
    return mocks


async def test_step_wanders_once_then_clicks_on_schedule(gesture) -> None:
    tick = Tick(MagicMock(spec=CDPPage))
    await tick.step()
    assert tick.seen and tick.attempts == 1
    gesture["wander"].assert_awaited_once()
    gesture["click"].assert_awaited_once_with(tick.page, BOX)
    await tick.step()
    assert tick.attempts == 1
    for _ in range(5):
        tick.next_click = 0.0
        await tick.step()
    assert tick.attempts == recaptcha.MAX_CLICKS


@pytest.mark.parametrize(
    "widget",
    [Widget("reCAPTCHA", BOX, True, False), Widget("hCaptcha", None, False, False), None],
)
async def test_step_leaves_a_ticked_undrawn_or_missing_checkbox(gesture, widget) -> None:
    gesture["locate"].return_value = widget
    tick = Tick(MagicMock(spec=CDPPage))
    await tick.step()
    assert tick.attempts == 0
    gesture["click"].assert_not_awaited()


async def test_step_gives_up_at_an_image_challenge(gesture) -> None:
    gesture["locate"].return_value = Widget("reCAPTCHA", BOX, False, True)
    tick = Tick(MagicMock(spec=CDPPage))
    await tick.step()
    assert tick.seen and tick.attempts == 0
    assert tick.deadline <= time.monotonic()
    gesture["click"].assert_not_awaited()


@pytest.mark.parametrize(
    ("after", "state"),
    [
        (Widget("reCAPTCHA", BOX, True, False), "checked"),
        (Widget("reCAPTCHA", BOX, False, True), "challenge"),
        (None, "gone"),
        (Widget("reCAPTCHA", BOX, False, False), "timeout"),
    ],
)
async def test_solve_clicks_once_and_reports_the_outcome(gesture, after, state) -> None:
    gesture["locate"].side_effect = [Widget("reCAPTCHA", BOX, False, False)] + [after] * 20
    result = await solve(MagicMock(spec=CDPPage), timeout_s=0.6)
    assert result == {"provider": "reCAPTCHA", "state": state}
    gesture["click"].assert_awaited_once()


@pytest.mark.parametrize(
    ("widget", "state"),
    [
        (None, "not_found"),
        (Widget("hCaptcha", None, False, False), "not_found"),
        (Widget("hCaptcha", BOX, True, False), "checked"),
        (Widget("hCaptcha", BOX, False, True), "challenge"),
    ],
)
async def test_solve_does_not_click_when_there_is_nothing_to_tick(gesture, widget, state) -> None:
    gesture["locate"].return_value = widget
    result = await solve(MagicMock(spec=CDPPage))
    assert result["state"] == state
    gesture["click"].assert_not_awaited()


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


async def test_settle_clicks_then_returns_the_page_the_callback_loads(gesture) -> None:
    real = {"status": 200, "headers": {}}
    page, events = _challenge_page([CHALLENGE, CHALLENGE, None])

    async def click(*_: Any) -> None:
        events.events += [
            ("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": "L2"}}),
            ("Network.responseReceived", {"loaderId": "L2", "type": "Document", "response": real}),
        ]

    gesture["click"].side_effect = click
    settled = await challenges.settle(
        page, events, {}, wait_until="load", deadline=time.monotonic() + 5, solve_recaptcha=True
    )
    assert settled is real
    gesture["click"].assert_awaited_once()


async def test_settle_stops_at_an_image_challenge_instead_of_waiting(gesture, monkeypatch) -> None:
    monkeypatch.setattr(challenges, "_POLL_S", 0.01)
    gesture["locate"].return_value = Widget("reCAPTCHA", BOX, False, True)
    page, events = _challenge_page([CHALLENGE] * 1000)
    started = time.monotonic()
    settled = await challenges.settle(
        page, events, {}, wait_until="load", deadline=time.monotonic() + 0.2, solve_recaptcha=True
    )
    assert settled == {"status": 403, "headers": {}}
    assert time.monotonic() - started < 2
    gesture["click"].assert_not_awaited()


@pytest.mark.parametrize("flags", [{}, {"solve_cloudflare": True, "solve_perimeterx": True}])
async def test_settle_leaves_the_checkbox_alone_without_the_flag(gesture, flags) -> None:
    page, events = _challenge_page([CHALLENGE])
    await challenges.settle(
        page, events, {}, wait_until="load", deadline=time.monotonic() + 30, **flags
    )
    gesture["locate"].assert_not_awaited()


@pytest.mark.parametrize("provider", ["reCAPTCHA", "hCaptcha"])
def test_checkbox_challenge_hints_name_the_flag(provider: str) -> None:
    hint = challenges.Detection(provider, "challenge").hint()
    assert hint.endswith("solve_recaptcha=True clicks its checkbox.")
    assert "solve_recaptcha" not in challenges.Detection("DataDome", "challenge").hint()


@pytest.mark.browser
@pytest.mark.parametrize(
    ("html", "provider"),
    [
        ('<div class="g-recaptcha" data-sitekey="k"></div>', "reCAPTCHA"),
        ('<div class="h-captcha" data-sitekey="k"></div>', "hCaptcha"),
        ('<script src="https://www.google.com/recaptcha/api.js" async></script>', "reCAPTCHA"),
        (
            '<script src="https://www.recaptcha.net/recaptcha/api.js?render=explicit"></script>',
            "reCAPTCHA",
        ),
        ('<script src="https://js.hcaptcha.com/1/api.js"></script>', "hCaptcha"),
        ('<iframe src="https://www.google.com/recaptcha/api2/anchor?k=x"></iframe>', "reCAPTCHA"),
        ('<script src="https://www.google.com/recaptcha/api.js?render=v3key"></script>', None),
        ('<script src="https://www.google.com.example.test/recaptcha/api.js"></script>', None),
        ('<div class="recaptcha-help">reCAPTCHA</div>', None),
    ],
)
async def test_a_checkbox_counts_as_a_challenge_only_on_a_blocking_status(
    html: str, provider: str | None, sandbox_supported: bool
) -> None:
    config = SessionConfig(chromium_sandbox=sandbox_supported)
    async with WebSkrapClient(default_config=config) as client:
        page = await (await client.session("markers")).new_page()
        await set_page_content(page, '<base href="https://example.test/">' + html)
        expected = challenges.Detection(provider, "challenge") if provider else None
        for status in (403, 429, 503):
            assert await detect(page, {}, status) == expected
        assert await detect(page, {}, 200) is None


GATE = """<html><head><title>Verify</title></head><body>
<div class="g-recaptcha" data-sitekey="test"></div>
<iframe src="https://www.google.com:{port}/recaptcha/api2/anchor?mode={mode}"
  width="304" height="78" style="border:0"></iframe>
<div style="position:absolute;top:-9999px" id="bframe-box">
<iframe src="https://www.google.com:{port}/recaptcha/api2/bframe" width="400" height="580"
  style="border:0"></iframe></div>
<script>addEventListener('message', (e) => {{
  if (e.data === 'cleared') {{ document.cookie = 'cleared=1; path=/'; location.reload(); }}
  if (e.data === 'challenge') document.getElementById('bframe-box').style.top = '120px';
}});</script></body></html>"""

ANCHOR = b"""<html><body style="margin:0">
<span id="recaptcha-anchor" role="checkbox" aria-checked="false" tabindex="0"
  style="position:absolute;left:13px;top:23px;width:28px;height:28px;border:2px solid #999"></span>
<script>
const mode = new URLSearchParams(location.search).get('mode');
const box = document.getElementById('recaptcha-anchor');
box.addEventListener('click', (e) => {
  fetch('/clicked?trusted=' + e.isTrusted + '&x=' + e.clientX + '&y=' + e.clientY).then(() => {
    if (mode === 'pass') {
      box.setAttribute('aria-checked', 'true');
      parent.postMessage('cleared', '*');
    }
    else parent.postMessage('challenge', '*');
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
        if url.path == "/clicked":
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            self.server.clicks.append(query)  # type: ignore[attr-defined]
            self._send(204, b"")
        elif url.path == "/recaptcha/api2/anchor":
            self._send(200, ANCHOR)
        elif url.path == "/recaptcha/api2/bframe":
            self._send(200, b"<html><body>Select all images with buses</body></html>")
        elif "cleared=1" in self.headers.get("Cookie", ""):
            self._send(200, b"<html><title>Real page</title><body>Welcome back</body></html>")
        else:
            port = self.server.widget_port  # type: ignore[attr-defined]
            body = GATE.format(port=port, mode=url.path.strip("/"))
            self._send(403, body.encode())

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def widget_server(tmp_path: Path) -> Iterator[ThreadingHTTPServer]:
    page, widget = (ThreadingHTTPServer(("127.0.0.1", 0), _Handler) for _ in range(2))
    widget.socket = tls_context(tmp_path, "www.google.com").wrap_socket(
        widget.socket, server_side=True
    )
    page.clicks = widget.clicks = []  # type: ignore[attr-defined]
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
        solve_recaptcha=True,
        launch_args=["--host-resolver-rules=MAP www.google.com 127.0.0.1"],
    )


@pytest.mark.browser
async def test_fetch_ticks_the_checkbox_and_returns_the_real_page(
    widget_server, sandbox_supported
) -> None:
    url = f"http://127.0.0.1:{widget_server.server_port}/pass"
    async with WebSkrapClient(default_config=_config(sandbox_supported)) as client:
        result = await client.fetch(url, text_only=True)
    assert (result.status, result.title, result.text) == (200, "Real page", "Welcome back")
    [click] = widget_server.clicks
    assert click["trusted"] == "true"
    assert 13 <= float(click["x"]) <= 45 and 23 <= float(click["y"]) <= 55


@pytest.mark.browser
async def test_fetch_stops_at_the_image_challenge_after_one_click(
    widget_server, sandbox_supported, monkeypatch
) -> None:
    monkeypatch.setattr(challenges, "INTERSTITIAL_TIMEOUT_S", 5.0)
    monkeypatch.setattr(recaptcha, "WANDER_S", (0.0, 0.1))
    url = f"http://127.0.0.1:{widget_server.server_port}/challenge"
    started = time.monotonic()
    async with WebSkrapClient(default_config=_config(sandbox_supported)) as client:
        with pytest.raises(WebSkrapError) as error:
            await client.fetch(url)
    assert error.value.code is ErrorCode.BLOCKED
    assert "reCAPTCHA" in str(error.value)
    assert len(widget_server.clicks) == 1
    assert time.monotonic() - started < 30
