"""Tests for persistent sessions on the CDP driver: transport, keys, and a live session."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import struct
import sys
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from test_cdp_browser import _CDPHandler

from webskrap import browser_session
from webskrap.cdp import _WEBSOCKET_GUID, WebSocketTransport, _mask
from webskrap.cdp_session import (
    PAGE_ORDER_FILE,
    TAB_SELECTION_FILE,
    _ordered_pages,
    _poll,
    _select,
    current_page,
    devtools_path,
    key_events,
)
from webskrap.errors import ErrorCode, WebSkrapError


def test_masking_round_trips() -> None:
    key = b"\x01\x02\x03\x04"
    payload = b'{"id": 1, "method": "Page.enable"}'
    assert _mask(_mask(payload, key), key) == payload
    assert _mask(b"", key) == b""


class _StreamWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    """A server frame: unmasked, as RFC 6455 requires from servers."""
    first = (0x80 if fin else 0) | opcode
    if len(payload) < 126:
        return struct.pack(">BB", first, len(payload)) + payload
    if len(payload) < 1 << 16:
        return struct.pack(">BBH", first, 126, len(payload)) + payload
    return struct.pack(">BBQ", first, 127, len(payload)) + payload


async def test_websocket_joins_fragments_and_answers_pings() -> None:
    reader = asyncio.StreamReader()
    writer = _StreamWriter()
    transport = WebSocketTransport(reader, writer)  # type: ignore[arg-type]
    big = b"x" * 70_000
    reader.feed_data(_frame(0x1, b'{"a":', fin=False))
    reader.feed_data(_frame(0x9, b"hi"))
    reader.feed_data(_frame(0x0, b"1}"))
    reader.feed_data(_frame(0x1, big))
    reader.feed_data(_frame(0x8, b""))

    assert await transport.read_message() == b'{"a":1}'
    # The ping between fragments got a masked pong with the same payload.
    assert writer.data[0] == 0x8A
    assert writer.data[1] == 0x80 | 2
    assert _mask(bytes(writer.data[6:8]), bytes(writer.data[2:6])) == b"hi"
    assert await transport.read_message() == big
    with pytest.raises(ConnectionError):
        await transport.read_message()


async def test_websocket_frames_are_masked_with_extended_lengths() -> None:
    writer = _StreamWriter()
    transport = WebSocketTransport(asyncio.StreamReader(), writer)  # type: ignore[arg-type]
    payload = b"y" * 300
    transport.write_message(payload)

    first, second, length = struct.unpack(">BBH", writer.data[:4])
    assert (first, second, length) == (0x81, 0x80 | 126, 300)
    assert _mask(bytes(writer.data[8:]), bytes(writer.data[4:8])) == payload
    await transport.close()
    assert writer.closed


async def test_websocket_handshake_checks_the_accept_key() -> None:
    accepts: list[bool] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = (await reader.readuntil(b"\r\n\r\n")).decode()
        key = next(
            line.split(":", 1)[1].strip()
            for line in request.split("\r\n")
            if line.lower().startswith("sec-websocket-key")
        )
        assert "origin:" not in request.lower()
        good = not accepts
        accepts.append(good)
        proof = hashlib.sha1((key + _WEBSOCKET_GUID).encode(), usedforsecurity=False).digest()
        accept = base64.b64encode(proof if good else b"wrong").decode()
        writer.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        writer.write(_frame(0x1, json.dumps({"id": 1, "result": {}}).encode()))
        await writer.drain()
        # Close this side too: since Python 3.12, leaving `async with server`
        # waits for every connection to close, so an open one hangs the test.
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        transport = await WebSocketTransport.connect("127.0.0.1", port, "/devtools/browser/x")
        assert json.loads(await transport.read_message()) == {"id": 1, "result": {}}
        await transport.close()
        with pytest.raises(ConnectionError, match="accept key"):
            await WebSocketTransport.connect("127.0.0.1", port, "/devtools/browser/x")


def test_devtools_path_must_match_the_session_port(tmp_path: Path) -> None:
    (tmp_path / "DevToolsActivePort").write_text("4242\n/devtools/browser/abc\n")
    assert devtools_path(tmp_path, 4242) == "/devtools/browser/abc"
    with pytest.raises(WebSkrapError) as error:
        devtools_path(tmp_path, 9999)
    assert error.value.code is ErrorCode.SESSION_UNREACHABLE


def test_page_order_keeps_creation_order_across_connections(tmp_path: Path) -> None:
    order = tmp_path / PAGE_ORDER_FILE
    assert _ordered_pages(order, ["b", "a"]) == ["b", "a"]
    # Later listings come back in any order; known pages keep their place.
    assert _ordered_pages(order, ["c", "a", "b"]) == ["b", "a", "c"]
    assert _ordered_pages(order, ["c", "b"]) == ["b", "c"]
    if sys.platform != "win32":  # Windows has no POSIX mode bits
        assert order.stat().st_mode & 0o777 == 0o600
    order.write_text("not json")
    assert _ordered_pages(order, ["x"]) == ["x"]


def test_current_page_follows_selection_then_newer_pages(tmp_path: Path) -> None:
    assert current_page(tmp_path, []) is None
    assert current_page(tmp_path, ["a", "b"]) == "b"  # newest, with no selection
    _select(tmp_path, "a", ["a", "b"])
    assert current_page(tmp_path, ["a", "b"]) == "a"
    # A page opened after the selection (a popup) wins until it closes.
    assert current_page(tmp_path, ["a", "b", "c"]) == "c"
    assert current_page(tmp_path, ["a", "b"]) == "a"
    # A closed selection falls back to the newest page.
    assert current_page(tmp_path, ["b"]) == "b"
    (tmp_path / TAB_SELECTION_FILE).write_text("not json")
    assert current_page(tmp_path, ["a", "b"]) == "b"


@pytest.mark.parametrize(
    ("action", "index", "url"),
    [
        pytest.param("open", None, None, id="unknown-action"),
        pytest.param("select", None, None, id="select-needs-index"),
        pytest.param("list", 0, None, id="list-takes-no-index"),
        pytest.param("new", 1, None, id="new-takes-no-index"),
        pytest.param("close", 0, "https://example.com", id="url-only-for-new"),
    ],
)
async def test_tab_arguments_are_checked_before_attaching(
    action: str, index: int | None, url: str | None
) -> None:
    with pytest.raises(WebSkrapError) as error:
        await browser_session.tabs("never-opened", action, index=index, url=url)
    assert error.value.code is ErrorCode.USAGE


async def test_poll_stops_a_stalled_check_at_the_deadline() -> None:
    async def stalled() -> bool:
        await asyncio.sleep(30)
        return True

    started = asyncio.get_running_loop().time()
    with pytest.raises(WebSkrapError) as error:
        await _poll(stalled, 200, "a stalled renderer")
    assert error.value.code is ErrorCode.TIMEOUT
    assert asyncio.get_running_loop().time() - started < 2


def _summary(events: list[dict[str, Any]]) -> list[tuple[str, str, int]]:
    return [(e["type"], e["key"], e["modifiers"]) for e in events]


def test_key_events_follow_playwright_combos() -> None:
    enter = key_events("Enter")
    assert _summary(enter) == [("keyDown", "Enter", 0), ("keyUp", "Enter", 0)]
    assert enter[0]["text"] == "\r"
    assert "text" not in enter[1]

    assert _summary(key_events("Shift+b")) == [
        ("rawKeyDown", "Shift", 8),
        ("keyDown", "B", 8),
        ("keyUp", "B", 8),
        ("keyUp", "Shift", 0),
    ]
    # A command chord inserts no text, so Control+a selects instead of typing.
    control_a = key_events("Control+a")
    assert control_a[1]["type"] == "rawKeyDown"
    assert "text" not in control_a[1]
    assert control_a[1]["code"] == "KeyA"
    assert [e["key"] for e in key_events("Shift++")][1] == "+"
    assert key_events("+")[0]["text"] == "+"
    assert key_events("F5")[0]["windowsVirtualKeyCode"] == 116
    # Shift maps through the US layout, not just upper(): 1 to !, - to _.
    for combo, key, code in (
        ("Shift+1", "!", "Digit1"),
        ("Shift+-", "_", "Minus"),
        ("?", "?", "Slash"),
    ):
        down = next(e for e in key_events(combo) if e["key"] != "Shift")
        assert (down["key"], down["text"], down["code"]) == (key, key, code)


@pytest.mark.parametrize("combo", ["Hyper+a", "NotAKey"])
def test_key_events_reject_unknown_names(combo: str) -> None:
    with pytest.raises(WebSkrapError) as error:
        key_events(combo)
    assert error.value.code is ErrorCode.USAGE


SESSION_PAGE = (
    b"<html><title>Session</title><body><h1>Hello session</h1>"
    b"<input id='q'><button id='b' style='width:200px;height:80px'>Go</button>"
    b"<p id='out'></p><script>window.appValue = 41 + 1;"
    b"b.addEventListener('click', e => out.textContent = 'clicked ' + e.isTrusted);"
    b"setTimeout(() => document.body.insertAdjacentHTML('beforeend', '<i id=late>Late</i>'), 300);"
    b"</script></body></html>"
)


FORM_PAGE = b"""<html><title>Form</title><body><h1>Form</h1>
<a href="/home"><img alt="Logo" src="data:,"></a>
<p>Run <code>make</code> first</p>
<label for="name">Name</label><input id="name" value="old">
<label><input type="checkbox" id="agree"> Agree</label>
<select id="color"><option value="r">Red</option><option value="g">Green</option></select>
<fieldset disabled><select id="locked"><option value="a">A</option><option value="b">B</option>
</select></fieldset>
<textarea id="notes" aria-label="Notes"></textarea>
<button id="go">Send</button>
<div hidden><button>Secret</button></div>
<div style="visibility:hidden">Invisible text
<span style="visibility:visible">Shown again</span></div>
<div style="height:3000px"></div>
<button id="far">Far</button>
<button class="dup">A</button><button class="dup">B</button>
<p id="log"></p>
<script>
window.__events = [];
for (const t of ['wheel', 'mousemove', 'mousedown', 'mouseup', 'keydown'])
  addEventListener(t, e => __events.push({t, ts: e.timeStamp, trusted: e.isTrusted}), true);
go.addEventListener('click', e => log.textContent += 'sent:' + e.isTrusted + ' ');
far.addEventListener('click', () => log.textContent += 'far ');
</script></body></html>"""


FRAMES_PAGE = """<html><title>Frames</title><body><h1>Top</h1>
<iframe title="Same" src="/frame/same"></iframe>
<div style="height:3000px"></div>
<iframe title="Cross" src="http://localhost:{port}/frame/cross"></iframe>
</body></html>"""

# Each frame reports its clicks and field value as text, so a snapshot of
# the top page reads them back.
FRAME_PAGE = """<html><body><button id="b">{name} button</button>
<input aria-label="{name} field"><p id="log"></p><script>
b.addEventListener('click', e => log.textContent += '{name}:' + e.isTrusted + ' ');
document.querySelector('input').addEventListener('input',
  e => log.textContent = 'value:' + e.target.value);
</script></body></html>"""


# An element four times wider than the iframe that shows it.
WIDE_FRAMES_PAGE = b"""<html><title>Wide</title><body style="margin:0">
<iframe title="Wide" src="/wide-inner" style="width:200px;height:100px;border:0"></iframe>
</body></html>"""
WIDE_FRAME_PAGE = b"""<html><body style="margin:0">
<div role="img" aria-label="Wide box" style="width:800px;height:50px;background:red"></div>
</body></html>"""


class _SessionHandler(_CDPHandler):
    def do_GET(self) -> None:
        if self.path == "/frames" or self.path.startswith("/frame/"):
            port = self.server.server_address[1]
            name = self.path.removeprefix("/frame/").capitalize()
            body = FRAMES_PAGE.format(port=port) if self.path == "/frames" else FRAME_PAGE
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.replace("{name}", name).encode())
            return
        if self.path in ("/wide-frame", "/wide-inner"):
            body = WIDE_FRAMES_PAGE if self.path == "/wide-frame" else WIDE_FRAME_PAGE
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/form":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(FORM_PAGE)
            return
        if self.path in ("/session", "/session-2"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(SESSION_PAGE.replace(b"Session<", self.path.encode() + b"<"))
            return
        super().do_GET()


@pytest.fixture()
def session_server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _SessionHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=3)
        httpd.server_close()


@pytest.mark.browser
def test_cdp_session_drives_pages_across_reconnects(
    persistent_session_env: Path, session_server: str, tmp_path: Path
) -> None:
    name = "cdp-session"

    async def exercise() -> dict[str, Any]:
        opened = await browser_session.open_session(name)
        results: dict[str, Any] = {"opened": opened}

        async def run(action: Any) -> Any:
            return await browser_session.run_page_action(name, action, timeout_ms=20_000)

        # Each run is a fresh attach, as each CLI command is a new process.
        results["goto"] = await run(
            lambda page: browser_session.goto(page, f"{session_server}/session", "load")
        )
        await run(lambda page: browser_session.goto(page, f"{session_server}/session-2", "load"))
        results["back"] = await run(lambda page: browser_session.history(page, "go_back"))
        await run(lambda page: browser_session.evaluate(page, "location.hash = 'part'"))
        # A same-document step fires no load event; it must still finish fast.
        started = asyncio.get_running_loop().time()
        await run(lambda page: browser_session.history(page, "go_back"))
        results["hash_back_s"] = asyncio.get_running_loop().time() - started
        results["hash_forward"] = await run(
            lambda page: browser_session.history(page, "go_forward")
        )
        results["main_world"] = await run(
            lambda page: browser_session.evaluate(page, "window.appValue")
        )
        results["function"] = await run(
            lambda page: browser_session.evaluate(page, "() => document.title")
        )
        results["serialized"] = [
            await run(
                lambda page, expression=expression: browser_session.evaluate(page, expression)
            )
            for expression in (
                "window",
                "document.body",
                "(() => { const o = {a: [1, 2]}; o.self = o; return o; })()",
                "1n + 2n",
                "async () => ({ when: new Date(0), nothing: undefined })",
            )
        ]
        results["text"] = await run(
            lambda page: browser_session.read_text(page, max_chars=5, offset=6)
        )
        results["wait"] = await run(
            lambda page: browser_session.wait_for(page, selector="#late", timeout_ms=5_000)
        )
        await run(
            lambda page: browser_session.evaluate(page, "document.getElementById('q').focus()")
        )
        await run(lambda page: browser_session.press(page, "Shift+h"))
        await run(lambda page: browser_session.insert_text(page, "i!"))
        results["typed"] = await run(
            lambda page: browser_session.evaluate(page, "document.getElementById('q').value")
        )
        view = await run(browser_session.view)
        results["view"] = (view.width, view.height, view.image[:8])
        box = await run(
            lambda page: browser_session.evaluate(
                page, "(() => { const r = b.getBoundingClientRect(); return [r.x, r.y]; })()"
            )
        )
        await run(
            lambda page: browser_session.mouse_action(page, "click", box[0] + 20, box[1] + 20)
        )
        results["clicked"] = await run(
            lambda page: browser_session.evaluate(
                page, "document.getElementById('out').textContent"
            )
        )
        results["waits"] = [
            (
                await run(
                    lambda page, kwargs=kwargs: browser_session.wait_for(
                        page, timeout_ms=5_000, **kwargs
                    )
                )
            )["matched"]
            for kwargs in (
                {"text": "hello SESSION"},
                {"text_gone": "never on the page"},
                {"selector": "#missing", "selector_state": "detached"},
                {"load_state": "networkidle"},
            )
        ]
        with pytest.raises(WebSkrapError) as timed_out:
            await run(lambda page: browser_session.wait_for(page, text="absent", timeout_ms=300))
        results["timed_out"] = timed_out.value.code
        await run(
            lambda page: browser_session.evaluate(
                page,
                "window.__mouse = []; for (const t of ['dblclick', 'mousedown', 'mouseup', "
                "'dragstart', 'drop', 'wheel']) "
                "addEventListener(t, e => __mouse.push(t + ':' + e.buttons), true);"
                "addEventListener('dragover', e => e.preventDefault())",
            )
        )
        for action, extra in (
            ("dblclick", {}),
            ("drag", {"end_x": 60.0, "end_y": 60.0}),
            ("scroll", {"delta_y": 120.0}),
        ):
            await run(
                lambda page, action=action, extra=extra: browser_session.mouse_action(
                    page, action, 30, 30, **extra
                )
            )
        # Wheel events reach the page asynchronously, after the input call returns.
        for _ in range(20):
            results["mouse"] = await run(
                lambda page: browser_session.evaluate(page, "window.__mouse")
            )
            if "wheel:0" in results["mouse"]:
                break
            await asyncio.sleep(0.1)
        shot = tmp_path / "full.png"
        await run(lambda page: browser_session.screenshot(page, shot, full_page=True))
        results["shot"] = shot.read_bytes()[:8]
        # A popup becomes the current page for the next command, as with
        # Playwright's pages[-1]; the opener stays reachable once it closes.
        await run(lambda page: browser_session.evaluate(page, "window.open('/session-2')"))
        results["popup"] = await run(browser_session.page_state)
        await run(lambda page: browser_session.evaluate(page, "window.close()"))
        results["after_popup"] = await run(browser_session.page_state)
        # eval carries a user gesture, as in Playwright, so a popup opens
        # without a click first; one gesture allows one popup.
        await run(lambda page: browser_session.goto(page, f"{session_server}/session", "load"))
        await run(lambda page: browser_session.evaluate(page, "window.open('/session-2?gesture')"))
        results["gesture_popup"] = await run(browser_session.page_state)
        await run(lambda page: browser_session.evaluate(page, "window.close()"))
        with pytest.raises(WebSkrapError) as mismatch:
            await browser_session.open_session(name, proxy_server="http://127.0.0.1:9")
        results["mismatch"] = mismatch.value.code
        results["reused"] = await browser_session.open_session(name)
        await run(
            lambda page: browser_session.goto(page, f"{session_server}/console-probe", "load")
        )
        results["probe"] = await run(
            lambda page: browser_session.wait_for(
                page, selector="#out:not(:empty)", timeout_ms=5_000
            )
        )
        results["probe_text"] = await run(
            lambda page: browser_session.read_text(page, max_chars=200)
        )
        return results

    try:
        results = asyncio.run(exercise())
    finally:
        browser_session.close_session(name, delete_data=True)

    assert results["opened"]["reused"] is False
    assert results["goto"]["status"] == 200
    assert results["goto"]["title"] == "/session"
    assert results["back"]["title"] == "/session"
    assert results["hash_back_s"] < 5
    assert results["hash_forward"]["url"].endswith("/session#part")
    assert results["main_world"] == 42  # page globals are visible to eval
    assert results["function"] == "/session"
    # Same placeholders WebSkrap 2.x reported for values JSON cannot carry.
    assert results["serialized"] == [
        "ref: <Window>",
        "ref: <Node>",
        {"a": [1, 2], "self": "[Circular]"},
        3,
        {"when": "1970-01-01T00:00:00.000Z", "nothing": None},
    ]
    assert results["text"]["text"] == "sessi"
    assert results["wait"]["matched"] == "selector"
    assert results["typed"] == "Hi!"
    width, height, magic = results["view"]
    assert magic == results["shot"] == b"\x89PNG\r\n\x1a\n"
    assert width > 0 and height > 0
    assert results["clicked"] == "clicked true"
    assert results["waits"] == ["text", "text_gone", "selector", "load_state"]
    assert results["timed_out"] is ErrorCode.TIMEOUT
    mouse = results["mouse"]
    assert mouse.count("dblclick:0") == 1
    # The drag starts on the word the double-click selected. Whether that
    # becomes a native drag depends on the platform (Linux Chromium: yes,
    # macOS: no), but either way the gesture must end, with a drop or a
    # mouseup, and later input must still reach the page.
    events = " ".join(mouse)
    drag = mouse[mouse.index("dblclick:0") + 1 :]
    assert drag[0] == "mousedown:1", events
    ends = [i for i, event in enumerate(drag) if event in ("drop:0", "mouseup:0")]
    assert ends and "wheel:0" in drag and ends[0] < drag.index("wheel:0"), events
    if sys.platform.startswith("linux"):
        assert "dragstart:1" in drag and "drop:0" in drag, events
    assert results["popup"]["url"].endswith("/session-2")
    assert results["after_popup"]["url"].endswith("/session#part")
    assert results["gesture_popup"]["url"].endswith("?gesture")
    assert results["mismatch"] is ErrorCode.USAGE
    assert results["reused"]["reused"] is True
    # Across all these attaches the page never saw Runtime or Console enabled.
    assert '"touched":false' in results["probe_text"]["text"]
    assert '"webdriver":false' in results["probe_text"]["text"]


def _ref(tree: str, line_start: str) -> str:
    line = next(line for line in tree.splitlines() if line.strip().startswith(line_start))
    match = re.search(r"\[ref=((?:f\d+)?e\d+)\]", line)
    assert match, line
    return match.group(1)


@pytest.mark.browser
def test_cdp_session_snapshot_refs_drive_elements(
    persistent_session_env: Path, session_server: str
) -> None:
    name = "cdp-elements"

    async def exercise() -> dict[str, Any]:
        await browser_session.open_session(name)
        results: dict[str, Any] = {}

        async def run(action: Any) -> Any:
            return await browser_session.run_page_action(name, action, timeout_ms=20_000)

        async def act(action: str, target: str, *values: str) -> None:
            await run(
                lambda page: browser_session.element_action(page, action, target, list(values))
            )

        async def value(script: str) -> Any:
            return await run(lambda page: browser_session.evaluate(page, script))

        await run(lambda page: browser_session.goto(page, f"{session_server}/form", "load"))
        tree = (await run(browser_session.snapshot))["snapshot"]
        results["tree"] = tree
        results["full"] = (await run(lambda page: browser_session.snapshot(page, depth=0)))[
            "snapshot"
        ]
        results["shallow"] = (await run(lambda page: browser_session.snapshot(page, depth=1)))[
            "snapshot"
        ]
        name_ref = _ref(tree, '- textbox "Name"')
        send_ref = _ref(tree, '- button "Send"')
        # Refs resolve on later connections: each run is a fresh attach.
        results["wait_ref"] = await run(
            lambda page: browser_session.wait_for(page, selector=name_ref, timeout_ms=5_000)
        )
        await act("fill", name_ref, "Ada")
        results["name"] = await value("document.getElementById('name').value")
        await act("type", "#notes", "hi!")
        results["notes"] = await value("document.getElementById('notes').value")
        await act("check", "#agree")
        results["checked"] = await value("document.getElementById('agree').checked")
        await act("uncheck", "#agree")
        results["unchecked"] = await value("document.getElementById('agree').checked")
        await act("select", "#color", "g")
        results["color"] = await value("document.getElementById('color').value")
        await act("select", "#color", "Red")
        results["color_label"] = await value("document.getElementById('color').value")
        await value("window.__events.length = 0")
        await act("click", send_ref)
        results["send_events"] = await value("window.__events")
        await act("click", "#far")
        results["log"] = await value("document.getElementById('log').textContent")
        results["far_events"] = await value("window.__events")
        with pytest.raises(WebSkrapError) as disabled:
            await browser_session.run_page_action(
                name,
                lambda page: browser_session.element_action(page, "select", "#locked", ["b"]),
                timeout_ms=600,
            )
        results["disabled"] = disabled.value.code
        results["locked"] = await value("document.getElementById('locked').value")
        # check refuses a non-checkbox before any click reaches it.
        with pytest.raises(WebSkrapError) as not_checkable:
            await act("check", "#far")
        results["not_checkable"] = not_checkable.value.code
        results["log_after_check"] = await value("document.getElementById('log').textContent")
        for target, code in ((".dup", ErrorCode.USAGE), ("##", ErrorCode.USAGE)):
            with pytest.raises(WebSkrapError) as error:
                await act("click", target)
            assert error.value.code is code
        await value("document.getElementById('go').remove()")
        with pytest.raises(WebSkrapError) as stale:
            await act("click", send_ref)
        results["stale"] = stale.value.code
        return results

    try:
        results = asyncio.run(exercise())
    finally:
        browser_session.close_session(name, delete_data=True)

    tree = results["tree"]
    assert '- heading "Form" [level=1] [ref=e' in tree
    assert '- textbox "Name" [ref=e' in tree and ': "old"' in tree
    assert '- checkbox "Agree" [ref=e' in tree
    assert "- combobox [ref=e" in tree and ': "Red"' in tree
    assert '- textbox "Notes" [ref=e' in tree
    assert "Secret" not in tree  # hidden subtrees are left out
    assert "Invisible text" not in tree and "Shown again" in tree
    # Links are named by their image's alt text and say where they go.
    assert '- link "Logo" [ref=e' in tree and "- /url: /home" in tree
    assert "- code [ref=e" in tree and "- text: Run" in tree
    assert "#" not in "".join(re.findall(r"\[ref=[^\]]*\]", tree))  # no placeholder left
    # Like Playwright, depth 0 means no limit; depth 1 keeps the top level.
    assert re.sub(r"e\d+", "e", results["full"]) == re.sub(r"e\d+", "e", tree)
    assert '- heading "Form"' in results["shallow"] and "  -" not in results["shallow"]
    assert '- button "Send"' in results["shallow"]
    assert results["wait_ref"]["matched"] == "selector"
    assert results["name"] == "Ada"  # fill replaced the old value
    assert results["notes"] == "hi!"
    assert (results["checked"], results["unchecked"]) == (True, False)
    assert (results["color"], results["color_label"]) == ("g", "r")
    events = results["send_events"]
    kinds = [event["t"] for event in events]
    assert all(event["trusted"] for event in events)
    first_down = kinds.index("mousedown")
    assert kinds[:first_down].count("mousemove") >= 10  # a path, not a teleport
    down, up = events[first_down], events[kinds.index("mouseup")]
    assert up["ts"] - down["ts"] >= 50  # a human button hold
    assert results["log"] == "sent:true far "
    assert [event["t"] for event in results["far_events"]].count("wheel") >= 5
    assert results["stale"] is ErrorCode.STALE_REF
    assert results["not_checkable"] is ErrorCode.USAGE
    assert results["log_after_check"] == "sent:true far "  # #far was not clicked
    # A disabled control is waited on, then refused, never changed.
    assert results["disabled"] is ErrorCode.TIMEOUT
    assert results["locked"] == "a"


@pytest.mark.browser
def test_cdp_session_snapshot_walks_into_iframes(
    persistent_session_env: Path, session_server: str
) -> None:
    name = "cdp-frames"

    async def exercise() -> dict[str, Any]:
        await browser_session.open_session(name)
        results: dict[str, Any] = {}

        async def run(action: Any) -> Any:
            return await browser_session.run_page_action(name, action, timeout_ms=20_000)

        async def act(action: str, target: str, *values: str) -> None:
            await run(
                lambda page: browser_session.element_action(page, action, target, list(values))
            )

        async def tree(depth: int | None = None) -> str:
            return str(
                (await run(lambda page: browser_session.snapshot(page, depth=depth)))["snapshot"]
            )

        await run(lambda page: browser_session.goto(page, f"{session_server}/frames", "load"))
        results["tree"] = first = await tree()
        results["shallow"] = await tree(depth=1)
        # 127.0.0.1 and localhost are different sites: the second frame runs
        # out of process, below the fold. Each run is a fresh attach.
        await act("click", _ref(first, '- button "Same button"'))
        await act("click", _ref(first, '- button "Cross button"'))
        results["clicked"] = await tree()
        await act("fill", _ref(first, '- textbox "Cross field"'), "typed")
        results["filled"] = await tree()
        with pytest.raises(WebSkrapError) as missing:
            await act("click", "f100000e1")  # keys stay below 100000
        results["missing"] = missing.value.code
        return results

    try:
        results = asyncio.run(exercise())
    finally:
        browser_session.close_session(name, delete_data=True)

    lines = results["tree"].splitlines()
    same = next(i for i, line in enumerate(lines) if line.startswith('- iframe "Same" [ref=e'))
    assert lines[same].endswith(":")
    assert re.fullmatch(r'  - button "Same button" \[ref=f\d+e\d+\]', lines[same + 1])
    assert re.search(r'^- iframe "Cross" \[ref=e\d+\]:$', results["tree"], re.M)
    assert re.search(r'^  - textbox "Cross field" \[ref=f\d+e\d+\]$', results["tree"], re.M)
    same_key = re.search(r"f(\d+)e", lines[same + 1])
    cross_key = re.search(r'"Cross button" \[ref=f(\d+)e', results["tree"])
    assert same_key and cross_key and same_key[1] != cross_key[1]
    assert "button" not in results["shallow"] and '- iframe "Cross"' in results["shallow"]
    assert "Same:true" in results["clicked"] and "Cross:true" in results["clicked"]
    assert "value:typed" in results["filled"]
    assert results["missing"] is ErrorCode.STALE_REF


@pytest.mark.browser
def test_cdp_session_tabs(persistent_session_env: Path, session_server: str) -> None:
    name = "cdp-tabs"

    async def exercise() -> dict[str, Any]:
        await browser_session.open_session(name)
        results: dict[str, Any] = {}

        async def run(action: Any) -> Any:
            return await browser_session.run_page_action(name, action, timeout_ms=20_000)

        async def tabs(action: str, **kwargs: Any) -> list[dict[str, Any]]:
            return (await browser_session.tabs(name, action, **kwargs))["tabs"]

        # No command has recorded the tab order yet; the new tab still sorts last.
        results["fresh"] = await tabs("new")
        await tabs("close")
        await run(lambda page: browser_session.goto(page, f"{session_server}/session", "load"))
        results["new"] = await browser_session.tabs(name, "new", url=f"{session_server}/session-2")
        results["on_new"] = await run(browser_session.page_state)
        results["selected"] = await tabs("select", index=0)
        results["on_first"] = await run(browser_session.page_state)
        # A popup becomes current, and closing it returns to the selection.
        await run(lambda page: browser_session.evaluate(page, "window.open('/session-2?popup')"))
        results["popup"] = await tabs("list")
        results["closed_popup"] = await tabs("close")
        results["closed_second"] = await tabs("close", index=1)
        with pytest.raises(WebSkrapError) as missing:
            await tabs("select", index=5)
        results["missing"] = missing.value.code
        return results

    try:
        results = asyncio.run(exercise())
    finally:
        browser_session.close_session(name, delete_data=True)

    def current(listing: list[dict[str, Any]]) -> list[int]:
        return [tab["index"] for tab in listing if tab["current"]]

    assert [tab["url"].removeprefix(session_server) for tab in results["new"]["tabs"]] == [
        "/session",
        "/session-2",
    ]
    assert len(results["fresh"]) == 2 and current(results["fresh"]) == [1]
    assert results["new"]["status"] == 200
    assert current(results["new"]["tabs"]) == [1]
    assert results["on_new"]["url"].endswith("/session-2")
    assert current(results["selected"]) == [0]
    assert results["on_first"]["url"].endswith("/session")
    assert len(results["popup"]) == 3 and current(results["popup"]) == [2]
    assert len(results["closed_popup"]) == 2 and current(results["closed_popup"]) == [0]
    assert [tab["url"].removeprefix(session_server) for tab in results["closed_second"]] == [
        "/session"
    ]
    assert results["missing"] is ErrorCode.USAGE


def _png_size(path: Path) -> tuple[int, int]:
    width, height = struct.unpack(">II", path.read_bytes()[16:24])
    return width, height


@pytest.mark.browser
def test_cdp_session_navigation_reports_and_reads(
    persistent_session_env: Path, session_server: str, tmp_path: Path
) -> None:
    name = "cdp-report"

    async def exercise() -> dict[str, Any]:
        await browser_session.open_session(name)
        results: dict[str, Any] = {}

        async def run(action: Any) -> Any:
            return await browser_session.run_page_action(name, action, timeout_ms=20_000)

        results["consent"] = await run(
            lambda page: browser_session.goto(
                page, f"{session_server}/cmp-banner", "load", decline_cookies=True
            )
        )
        results["plain"] = await run(
            lambda page: browser_session.goto(page, f"{session_server}/session", "load")
        )
        results["blocked"] = await run(
            lambda page: browser_session.goto(page, f"{session_server}/cf-block", "load")
        )
        await run(lambda page: browser_session.goto(page, f"{session_server}/form", "load"))
        results["grep"] = await run(
            lambda page: browser_session.search_text(page, "(?i)agree|send", context=1)
        )
        results["dpr"] = await run(lambda page: browser_session.evaluate(page, "devicePixelRatio"))
        results["button"] = await run(
            lambda page: browser_session.screenshot(
                page, tmp_path / "go.png", full_page=False, target="#go"
            )
        )
        await run(lambda page: browser_session.goto(page, f"{session_server}/frames", "load"))
        tree = (await run(browser_session.snapshot))["snapshot"]
        results["frame"] = await run(
            lambda page: browser_session.screenshot(
                page,
                tmp_path / "frame.png",
                full_page=False,
                target=_ref(tree, '- button "Cross button"'),
            )
        )
        await run(lambda page: browser_session.goto(page, f"{session_server}/wide-frame", "load"))
        wide_tree = (await run(browser_session.snapshot))["snapshot"]
        results["wide"] = await run(
            lambda page: browser_session.screenshot(
                page,
                tmp_path / "wide.png",
                full_page=False,
                target=_ref(wide_tree, '- img "Wide box"'),
            )
        )
        with pytest.raises(WebSkrapError) as both:
            await run(
                lambda page: browser_session.screenshot(
                    page, tmp_path / "x.png", full_page=True, target="h1"
                )
            )
        results["both"] = both.value.code
        return results

    try:
        results = asyncio.run(exercise())
    finally:
        browser_session.close_session(name, delete_data=True)

    assert results["consent"]["cookie_notice_declined"] == "cmp"
    assert results["consent"]["blocked"] is None
    assert "cookie_notice_declined" not in results["plain"]
    blocked = results["blocked"]
    assert blocked["status"] == 403
    assert blocked["blocked"]["provider"] == "Cloudflare"
    assert blocked["blocked"]["kind"] == "block"
    assert "0123456789abcdef" not in json.dumps(blocked["blocked"])
    grep = results["grep"]
    # innerText keeps inline controls on one line: "Name  Agree", then "Send".
    texts = [match["text"] for match in grep["matches"]]
    assert grep["matches_total"] == len(texts) >= 2
    assert "Agree" in texts[0] and any("Send" in text for text in texts[1:])
    assert grep["matches"][0]["after"] and len(grep["matches"][0]["after"]) == 1
    scale = results["dpr"]
    for key, path in (("button", tmp_path / "go.png"), ("frame", tmp_path / "frame.png")):
        clip = results[key]["clip"]
        width, height = _png_size(path)
        assert abs(width - clip["width"] * scale) <= 1, key
        assert abs(height - clip["height"] * scale) <= 1, key
    assert results["both"] is ErrorCode.USAGE
    # Cut to the iframe's content box, not the element's full width.
    assert results["wide"]["clip"]["width"] == 200
    assert results["wide"]["clip"]["height"] == 50
