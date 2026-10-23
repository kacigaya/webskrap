"""Tests for persistent sessions on the CDP driver: transport, keys, and a live session."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
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
from webskrap.cdp_session import PAGE_ORDER_FILE, _devtools_path, _ordered_pages, _poll, key_events
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
    assert _devtools_path(tmp_path, 4242) == "/devtools/browser/abc"
    with pytest.raises(WebSkrapError) as error:
        _devtools_path(tmp_path, 9999)
    assert error.value.code is ErrorCode.SESSION_UNREACHABLE


def test_page_order_keeps_creation_order_across_connections(tmp_path: Path) -> None:
    order = tmp_path / PAGE_ORDER_FILE
    assert _ordered_pages(order, ["b", "a"]) == ["b", "a"]
    # Later listings come back in any order; known pages keep their place.
    assert _ordered_pages(order, ["c", "a", "b"]) == ["b", "a", "c"]
    assert _ordered_pages(order, ["c", "b"]) == ["b", "c"]
    assert order.stat().st_mode & 0o777 == 0o600
    order.write_text("not json")
    assert _ordered_pages(order, ["x"]) == ["x"]


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


class _SessionHandler(_CDPHandler):
    def do_GET(self) -> None:
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
@pytest.mark.skipif(sys.platform == "win32", reason="the CDP driver needs posix_spawn")
def test_cdp_session_drives_pages_across_reconnects(
    persistent_session_env: Path, session_server: str, tmp_path: Path
) -> None:
    name = "cdp-session"

    async def exercise() -> dict[str, Any]:
        opened = await browser_session.open_session(name, driver="cdp")
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
        for refused in (
            lambda page: browser_session.snapshot(page),
            lambda page: browser_session.element_action(page, "click", "#b", []),
        ):
            with pytest.raises(WebSkrapError) as error:
                await run(refused)
            assert error.value.code is ErrorCode.USAGE
        # A popup becomes the current page for the next command, as with
        # Patchright's pages[-1]; the opener stays reachable once it closes.
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
            await browser_session.open_session(name, driver="patchright")
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

    assert results["opened"]["driver"] == "cdp"
    assert results["goto"]["status"] == 200
    assert results["goto"]["title"] == "/session"
    assert results["back"]["title"] == "/session"
    assert results["hash_back_s"] < 5
    assert results["hash_forward"]["url"].endswith("/session#part")
    assert results["main_world"] == 42  # page globals are visible to eval
    assert results["function"] == "/session"
    # Same placeholders Patchright reports for values JSON cannot carry.
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
    # The drag starts on the word the double-click selected, so it becomes a
    # native drag: the page sees dragstart and drop, and input keeps working.
    assert "dragstart:1" in mouse
    assert "drop:0" in mouse and "wheel:0" in mouse, " ".join(mouse)
    assert mouse.index("drop:0") < mouse.index("wheel:0")
    assert results["popup"]["url"].endswith("/session-2")
    assert results["after_popup"]["url"].endswith("/session#part")
    assert results["gesture_popup"]["url"].endswith("?gesture")
    assert results["mismatch"] is ErrorCode.USAGE
    assert results["reused"]["reused"] is True
    assert results["reused"]["driver"] == "cdp"
    # Across all these attaches the page never saw Runtime or Console enabled.
    assert '"touched":false' in results["probe_text"]["text"]
    assert '"webdriver":false' in results["probe_text"]["text"]
