from __future__ import annotations

import os
import sys
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

from webskrap import SessionConfig, WebSkrapClient, challenges, turnstile
from webskrap.cdp import CDPPage
from webskrap.cdp_dom import Frame
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.turnstile import Checkbox, find_checkbox, locate, square

CHALLENGE = {"provider": "Cloudflare", "kind": "challenge", "reloads": False}
BOX = {"x": 49.0, "y": 94.5, "width": 24.0, "height": 24.0}


def _node(node_id: int, name: str, *attrs: str, **kids: Any) -> dict[str, Any]:
    return {"nodeId": node_id, "nodeType": 1, "nodeName": name, "attributes": list(attrs), **kids}


WIDGET_DOCUMENT = _node(
    1,
    "HTML",
    children=[
        _node(2, "HEAD", children=[_node(3, "INPUT", "type", "hidden")]),
        _node(
            4,
            "BODY",
            shadowRoots=[
                {
                    "nodeId": 5,
                    "nodeType": 11,
                    "shadowRootType": "closed",
                    "children": [
                        _node(
                            6,
                            "LABEL",
                            "class",
                            "_a1",
                            children=[
                                _node(7, "INPUT", "type", "checkbox", "aria-label", "x"),
                                _node(8, "SPAN"),
                            ],
                        )
                    ],
                }
            ],
        ),
    ],
)


def test_finds_the_checkbox_under_a_closed_shadow_root() -> None:
    assert find_checkbox(WIDGET_DOCUMENT) == [7, 6]
    assert find_checkbox(_node(1, "HTML", children=[_node(2, "INPUT", "type", "text")])) == []


def test_aims_at_the_left_square_of_a_stretched_input() -> None:
    assert square([9, 20.5, 181.7, 20.5, 181.7, 44.5, 9, 44.5]) == {
        "x": 9,
        "y": 20.5,
        "width": 24.0,
        "height": 24.0,
    }
    assert square([10, 10, 30, 10, 30, 30, 10, 30])["width"] == 20
    assert square([10, 10, 11, 10, 11, 11, 10, 11]) is None


def _frame(page: CDPPage, session: str, url: str, owner: bool = True) -> Frame:
    frame_page = MagicMock(spec=CDPPage)
    frame_page.session_id = session
    return Frame(frame_page, page, url, 40.0, 74.0, ("S0", 9) if owner else None)


async def test_locate_measures_only_the_cross_site_widget_frame(monkeypatch) -> None:
    page = MagicMock(spec=CDPPage)
    page.session_id = "S0"
    main = _frame(page, "S0", "https://example.test/", owner=False)
    in_process = _frame(page, "S0", "https://challenges.cloudflare.com/a")
    other = _frame(page, "S1", "https://ads.example.test/frame")
    widget = _frame(page, "S2", "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x")
    for frame in (main, in_process, other):
        frame.page.send = AsyncMock(side_effect=AssertionError("measured the wrong frame"))

    async def widget_send(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "DOM.getDocument":
            assert params == {"depth": -1, "pierce": True}
            return {"root": WIDGET_DOCUMENT}
        if params["nodeId"] == 7:
            raise WebSkrapError("Could not compute box model.")
        return {"model": {"content": [9, 20.5, 181.7, 20.5, 181.7, 44.5, 9, 44.5]}}

    widget.page.send = AsyncMock(side_effect=widget_send)
    monkeypatch.setattr(
        turnstile.cdp_dom, "frames", AsyncMock(return_value=[main, in_process, other, widget])
    )
    assert await locate(page) == {"x": 49.0, "y": 94.5, "width": 24.0, "height": 24.0}


async def test_locate_waits_while_the_widget_verifies(monkeypatch) -> None:
    page = MagicMock(spec=CDPPage)
    page.session_id = "S0"
    widget = _frame(page, "S2", "https://challenges.cloudflare.com/x")
    widget.page.send = AsyncMock(
        side_effect=[{"root": WIDGET_DOCUMENT}, WebSkrapError("no box"), WebSkrapError("no box")]
    )
    monkeypatch.setattr(turnstile.cdp_dom, "frames", AsyncMock(return_value=[widget]))
    assert await locate(page) is None
    monkeypatch.setattr(
        turnstile.cdp_dom, "frames", AsyncMock(side_effect=WebSkrapError("navigated"))
    )
    assert await locate(page) is None


@pytest.fixture
def gesture(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    mocks = {
        "locate": AsyncMock(return_value=BOX),
        "wander": AsyncMock(),
        "click": AsyncMock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(turnstile, name, mock)
    return mocks


async def test_step_wanders_once_then_clicks_on_schedule(gesture) -> None:
    checkbox = Checkbox(MagicMock(spec=CDPPage))
    await checkbox.step()
    assert checkbox.seen and checkbox.clicks == 1
    gesture["wander"].assert_awaited_once()
    gesture["click"].assert_awaited_once_with(checkbox.page, BOX)
    assert checkbox.next_click > time.monotonic() + turnstile.CLICK_INTERVAL_S - 1
    await checkbox.step()
    assert checkbox.clicks == 1
    for _ in range(5):
        checkbox.next_click = 0.0
        await checkbox.step()
    assert checkbox.clicks == turnstile.MAX_CLICKS
    assert gesture["wander"].await_count == 1


async def test_step_does_nothing_without_a_checkbox_or_past_the_deadline(gesture) -> None:
    gesture["locate"].return_value = None
    checkbox = Checkbox(MagicMock(spec=CDPPage))
    await checkbox.step()
    assert not checkbox.seen and checkbox.clicks == 0
    gesture["locate"].return_value = BOX
    checkbox.deadline = time.monotonic() - 1
    await checkbox.step()
    assert checkbox.clicks == 0
    gesture["click"].assert_not_awaited()


async def test_a_gesture_cut_short_by_navigation_still_counts(gesture) -> None:
    gesture["click"].side_effect = WebSkrapError("page navigated")
    checkbox = Checkbox(MagicMock(spec=CDPPage))
    await checkbox.step()
    assert checkbox.clicks == 1


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


async def test_settle_clicks_then_returns_the_page_the_challenge_reloads_into(gesture) -> None:
    real = {"status": 200, "headers": {"server": "cloudflare"}}
    page, events = _challenge_page([CHALLENGE, CHALLENGE, None])

    async def click(*_: Any) -> None:
        events.events += [
            ("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": "L2"}}),
            ("Network.responseReceived", {"loaderId": "L2", "type": "Document", "response": real}),
        ]

    gesture["click"].side_effect = click
    settled = await challenges.settle(
        page, events, {}, wait_until="load", deadline=time.monotonic() + 5, solve_cloudflare=True
    )
    assert settled is real
    gesture["click"].assert_awaited_once()
    page.wait_lifecycle.assert_awaited_once()


async def test_settle_stops_after_the_last_click_and_its_budget(gesture, monkeypatch) -> None:
    monkeypatch.setattr(challenges, "_POLL_S", 0.01)
    monkeypatch.setattr(turnstile, "TIMEOUT_S", 0.4)
    monkeypatch.setattr(turnstile, "CLICK_INTERVAL_S", 0.05)
    page, events = _challenge_page([CHALLENGE] * 1000)
    started = time.monotonic()
    settled = await challenges.settle(
        page,
        events,
        {},
        wait_until="load",
        deadline=time.monotonic() + 0.05,
        solve_cloudflare=True,
    )
    assert settled == {"status": 403, "headers": {}}
    assert gesture["click"].await_count == turnstile.MAX_CLICKS
    assert time.monotonic() - started < 2


async def test_settle_without_a_drawn_checkbox_keeps_the_caller_deadline(gesture) -> None:
    gesture["locate"].return_value = None
    page, events = _challenge_page([CHALLENGE] * 1000)
    started = time.monotonic()
    await challenges.settle(
        page, events, {}, wait_until="load", deadline=time.monotonic() + 0.3, solve_cloudflare=True
    )
    assert time.monotonic() - started < 2
    gesture["click"].assert_not_awaited()


async def test_settle_leaves_cloudflare_alone_without_the_flag(gesture) -> None:
    page, events = _challenge_page([CHALLENGE])
    settled = await challenges.settle(
        page, events, {}, wait_until="load", deadline=time.monotonic() + 30
    )
    assert settled == {"status": 403, "headers": {}}
    gesture["locate"].assert_not_awaited()


def test_cloudflare_challenge_hints_name_the_flag() -> None:
    challenge = challenges.Detection("Cloudflare", "challenge")
    assert challenge.hint().endswith("solve_cloudflare=True clicks its Turnstile checkbox.")
    assert "solve_cloudflare" in challenge.hint(browser_session=True)
    assert "solve_cloudflare" not in challenges.Detection("DataDome", "challenge").hint()
    assert "solve_cloudflare" not in challenges.Detection("Cloudflare", "block").hint()


INTERSTITIAL = """<html><head><title>Just a moment...</title>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script></head>
<body><p>Verifying you are human.</p>
<iframe src="https://challenges.cloudflare.com:{port}/widget?mode={mode}"
  width="300" height="65" style="border:0"></iframe>
<script>addEventListener('message', (e) => {{
  if (e.data === 'cleared') {{ document.cookie = 'cleared=1; path=/'; location.reload(); }}
}});</script></body></html>"""

WIDGET = b"""<html><body style="margin:0"><script>
const mode = new URLSearchParams(location.search).get('mode');
const root = document.body.attachShadow({mode: 'closed'});
root.innerHTML = '<label style="display:none;position:relative;align-items:center;'
  + 'margin:20px 9px;width:172px;height:24px;gap:8px">'
  + '<input type="checkbox" style="position:absolute;inset:0;margin:0;opacity:0;'
  + 'width:100%;height:100%"><span style="width:20px;height:20px;border:2px solid #555">'
  + '</span><span>Verify you are human</span></label>';
setTimeout(() => { root.querySelector('label').style.display = 'flex'; }, 700);
root.querySelector('input').addEventListener('click', (e) => {
  fetch('/clicked?x=' + e.clientX + '&y=' + e.clientY + '&trusted=' + e.isTrusted)
    .then(() => { if (mode === 'pass') parent.postMessage('cleared', '*'); });
});
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    clicks: list[dict[str, str]]

    def _send(self, status: int, body: bytes, **headers: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        if url.path == "/clicked":
            self.server.clicks.append(query)  # type: ignore[attr-defined]
            self._send(204, b"")
        elif url.path == "/widget":
            self._send(200, WIDGET)
        elif url.path.startswith("/cdn-cgi/"):
            self._send(404, b"")
        elif "cleared=1" in self.headers.get("Cookie", ""):
            self._send(200, b"<html><title>Real page</title><body>Welcome back</body></html>")
        else:
            port = self.server.widget_port  # type: ignore[attr-defined]
            body = INTERSTITIAL.format(port=port, mode=url.path.strip("/"))
            self._send(403, body.encode(), cf_mitigated="challenge")

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def widget_server(tmp_path: Path) -> Iterator[ThreadingHTTPServer]:
    page, widget = (ThreadingHTTPServer(("127.0.0.1", 0), _Handler) for _ in range(2))
    widget.socket = tls_context(tmp_path, turnstile.FRAME_HOST).wrap_socket(
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


def _modes() -> list[Any]:
    has_desktop = sys.platform in ("darwin", "win32") or bool(os.environ.get("DISPLAY"))
    return [
        pytest.param({}, id="headless"),
        pytest.param(
            {"virtual_display": True},
            id="virtual-display",
            marks=pytest.mark.skipif(
                not challenges.xvfb_available(), reason="virtual_display needs Xvfb"
            ),
        ),
        pytest.param(
            {"headless": False},
            id="headed",
            marks=pytest.mark.skipif(not has_desktop, reason="headed needs a desktop"),
        ),
    ]


def _config(sandbox_supported: bool, mode: dict[str, Any]) -> SessionConfig:
    return SessionConfig(
        channel="chromium",
        chromium_sandbox=sandbox_supported,
        ignore_https_errors=True,
        solve_cloudflare=True,
        launch_args=["--host-resolver-rules=MAP challenges.cloudflare.com 127.0.0.1"],
        **mode,
    )


@pytest.mark.browser
@pytest.mark.parametrize("mode", _modes())
async def test_fetch_clicks_the_drawn_checkbox_and_returns_the_real_page(
    widget_server, sandbox_supported, mode
) -> None:
    url = f"http://127.0.0.1:{widget_server.server_port}/pass"
    async with WebSkrapClient(default_config=_config(sandbox_supported, mode)) as client:
        result = await client.fetch(url, text_only=True)
    assert (result.status, result.title, result.text) == (200, "Real page", "Welcome back")
    [click] = widget_server.clicks
    assert click["trusted"] == "true"
    assert 9 <= float(click["x"]) <= 33 and 20 <= float(click["y"]) <= 44


@pytest.mark.browser
async def test_fetch_reports_a_checkbox_that_never_clears(
    widget_server, sandbox_supported, monkeypatch
) -> None:
    monkeypatch.setattr(challenges, "INTERSTITIAL_TIMEOUT_S", 5.0)
    monkeypatch.setattr(turnstile, "TIMEOUT_S", 12.0)
    monkeypatch.setattr(turnstile, "CLICK_INTERVAL_S", 1.0)
    monkeypatch.setattr(turnstile, "WANDER_S", (0.0, 0.1))
    url = f"http://127.0.0.1:{widget_server.server_port}/stay"
    started = time.monotonic()
    async with WebSkrapClient(default_config=_config(sandbox_supported, {})) as client:
        with pytest.raises(WebSkrapError) as error:
            await client.fetch(url)
    assert error.value.code is ErrorCode.BLOCKED
    assert len(widget_server.clicks) == turnstile.MAX_CLICKS
    assert time.monotonic() - started < 45
