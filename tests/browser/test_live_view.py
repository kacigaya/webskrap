from __future__ import annotations

import asyncio
import base64
import math
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from webskrap.browser import live_view
from webskrap.driver.cdp import CDPConnection, CDPPage
from webskrap.errors import ErrorCode, WebSkrapError


@pytest.mark.parametrize(
    ("allow_scroll", "allow_control"), [(False, False), (True, False), (False, True)]
)
async def test_live_view_requires_token_and_stays_with_original_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allow_scroll: bool, allow_control: bool
) -> None:
    state = {"pid": 42, "port": 9222}
    monkeypatch.setattr(
        live_view.browser_session, "_open_state", lambda _: (tmp_path, state.copy())
    )
    monkeypatch.setattr(live_view, "FRAME_WAIT_S", 0.05)
    monkeypatch.setattr(live_view, "_IDLE_S", math.inf)
    connection = AsyncMock(spec=CDPConnection)
    clicks: list[object] = []
    casts: list[object] = []
    acks: list[object] = []
    handlers: dict[str, Callable[[dict[str, object]], None]] = {}
    connection.on.side_effect = lambda method, handler, **_: handlers.__setitem__(method, handler)

    def emit(frame: bytes, session_id: int) -> None:
        handlers["Page.screencastFrame"](
            {"data": base64.b64encode(frame).decode(), "sessionId": session_id}
        )

    def send(method: str, params: object = None, **_: object) -> dict[str, object]:
        match method:
            case "Target.getTargets":
                return {"targetInfos": [{"type": "page", "targetId": "tab"}]}
            case "Target.attachToTarget":
                return {"sessionId": "attached"}
            case "Page.stopScreencast":
                return {}
            case "Page.startScreencast":
                casts.append(params)
                emit(b"\x89PNG\r\n\x1a\nfirst", 1)
                return {}
            case "Page.screencastFrameAck":
                acks.append(params)
                return {}
            case "Input.dispatchMouseEvent":
                clicks.append(params)
                return {}
            case "Input.insertText":
                assert params == {"text": "A"}
                return {}
            case "Target.detachFromTarget":
                return {}
            case _:
                raise AssertionError(f"Unexpected CDP command: {method}")

    connection.send.side_effect = send

    @asynccontextmanager
    async def connect(
        name: str, directory: Path, port: int, *, timeout_ms: float
    ) -> AsyncIterator[CDPConnection]:
        assert (name, directory, port) == ("visual", tmp_path / "user-data", 9222)
        assert timeout_ms > 0
        yield connection

    monkeypatch.setattr(live_view.targets, "connect", connect)

    async def viewport(_: CDPPage) -> dict[str, int]:
        return {"width": 100, "height": 200}

    monkeypatch.setattr(live_view.cdp_session, "viewport", viewport)
    url, task = await live_view.start(
        "visual", allow_scroll=allow_scroll, allow_control=allow_control
    )
    address = urlsplit(url)
    assert address.hostname == "127.0.0.1"

    def request(
        path: str,
        token: str = "",
        method: str = "GET",
        *,
        body: bytes | None = None,
        target: str = "tab",
    ) -> tuple[int, dict[str, str], bytes]:
        conn = HTTPConnection(address.hostname, address.port, timeout=2)
        try:
            conn.request(
                method,
                path,
                body=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-WebSkrap-Target": target,
                },
            )
            reply = conn.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read()
        finally:
            conn.close()

    try:
        status, headers, html = await asyncio.to_thread(request, "/")
        assert (status, headers["Cache-Control"]) == (200, "no-store")
        assert b"WebSkrap Live View" in html
        assert address.fragment.encode() not in html
        assert (await asyncio.to_thread(request, "/frame?after=0", "wrong"))[0] == 404
        assert (await asyncio.to_thread(request, "/frame", address.fragment))[0] == 400
        assert (await asyncio.to_thread(request, "/frame?after=-1", address.fragment))[0] == 400
        async with asyncio.timeout(2):
            while not casts:
                await asyncio.sleep(0.01)
        assert casts == [{"format": "png"}]
        status, headers, body = await asyncio.to_thread(request, "/frame?after=0", address.fragment)
        assert (status, body) == (200, b"\x89PNG\r\n\x1a\nfirst")
        assert headers["Content-Type"] == "image/png"
        assert headers["Cache-Control"] == "no-store"
        assert (headers["X-WebSkrap-Frame"], headers["X-WebSkrap-Target"]) == ("1", "tab")
        assert (await asyncio.to_thread(request, "/frame?after=1", address.fragment))[0] == 204
        emit(b"second", 2)
        status, headers, body = await asyncio.to_thread(request, "/frame?after=1", address.fragment)
        assert (status, body, headers["Content-Type"]) == (200, b"second", "image/jpeg")
        assert headers["X-WebSkrap-Frame"] == "2"
        scroll = "/scroll?x=.25&y=.5&dx=0&dy=120"
        assert (await asyncio.to_thread(request, scroll, "wrong", "POST"))[0] == 404
        assert (await asyncio.to_thread(request, scroll, address.fragment, "POST"))[0] == (
            204 if allow_scroll or allow_control else 404
        )
        if allow_scroll or allow_control:
            bad = "/scroll?x=.25&y=.5&dx=0&dy=nan"
            assert (await asyncio.to_thread(request, bad, address.fragment, "POST"))[0] == 400
        click = "/click?x=.25&y=.5"
        assert (await asyncio.to_thread(request, click, "wrong", "POST"))[0] == 404
        assert (await asyncio.to_thread(request, click, address.fragment, "POST"))[0] == (
            204 if allow_control else 404
        )
        if allow_control:
            assert (
                await asyncio.to_thread(request, click, address.fragment, "POST", target="old")
            )[0] == 409
            assert (await asyncio.to_thread(request, "/key", address.fragment, "POST", body=b"A"))[
                0
            ] == 204
            assert (
                await asyncio.to_thread(
                    request, "/key", address.fragment, "POST", body=b"Control+a"
                )
            )[0] == 400
        state["pid"] += 1
        if allow_scroll or allow_control:
            assert (await asyncio.to_thread(request, scroll, address.fragment, "POST"))[0] == 410
        expected: list[object] = []
        if allow_scroll or allow_control:
            expected.append({"type": "mouseWheel", "x": 25, "y": 100, "deltaX": 0, "deltaY": 120})
        if allow_control:
            for kind in ("mousePressed", "mouseReleased"):
                expected.append(
                    {"type": kind, "x": 25, "y": 100, "button": "left", "clickCount": 1}
                )
        assert clicks == expected
        assert (await asyncio.to_thread(request, "/frame?after=0", address.fragment))[0] == 410
        assert {"sessionId": 1} in acks
        assert not list(tmp_path.iterdir())
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with pytest.raises(OSError):
        await asyncio.to_thread(request, "/")


async def test_live_view_reports_missing_session(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_: str) -> None:
        raise WebSkrapError("session is not open", code=ErrorCode.NO_SESSION)

    monkeypatch.setattr(live_view.browser_session, "_open_state", missing)
    with pytest.raises(WebSkrapError, match="session is not open"):
        await live_view.start("missing")


async def test_live_view_streams_motion_as_jpeg_and_follows_the_current_tab(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name, value in (
        ("_FRAME_PACE_S", 0),
        ("_SETTLE_S", 0.05),
        ("_TAB_CHECK_S", 0.02),
        ("_IDLE_S", math.inf),
    ):
        monkeypatch.setattr(live_view, name, value)
    connection = AsyncMock(spec=CDPConnection)
    handlers: dict[str, Callable[[dict[str, object]], None]] = {}
    connection.on.side_effect = lambda method, handler, **_: handlers.__setitem__(method, handler)
    casts: list[object] = []
    pages = ["tab"]

    def send(method: str, params: object = None, **_: object) -> dict[str, object]:
        if method == "Page.startScreencast":
            casts.append(params)
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": page} for page in pages]}
        return {}

    connection.send.side_effect = send
    feed = live_view._Feed()
    cast = asyncio.create_task(
        live_view._cast(CDPPage(connection, "attached", "tab"), tmp_path, feed, lambda: None)
    )
    async with asyncio.timeout(2):
        while casts != [{"format": "png"}]:
            await asyncio.sleep(0.01)
        assert feed.page is not None
        for number in range(3):
            handlers["Page.screencastFrame"]({"data": "AA==", "sessionId": number})
        while len(casts) < 3:
            await asyncio.sleep(0.01)
        assert casts[1:] == [{"format": "jpeg", "quality": 85}, {"format": "png"}]
        assert (feed.number, feed.target) == (3, "tab")
        pages.append("popup")
        await cast
    assert feed.page is None
    connection.off.assert_called_once()


async def test_live_view_pauses_the_screencast_without_viewers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name, value in (("_FRAME_PACE_S", 0), ("_IDLE_S", 0.05), ("_TAB_CHECK_S", 10)):
        monkeypatch.setattr(live_view, name, value)
    connection = AsyncMock(spec=CDPConnection)
    handlers: dict[str, Callable[[dict[str, object]], None]] = {}
    connection.on.side_effect = lambda method, handler, **_: handlers.__setitem__(method, handler)
    sent: list[str] = []

    def send(method: str, params: object = None, **_: object) -> dict[str, object]:
        sent.append(method)
        return {}

    connection.send.side_effect = send
    feed = live_view._Feed()
    cast = asyncio.create_task(
        live_view._cast(CDPPage(connection, "attached", "tab"), tmp_path, feed, lambda: None)
    )
    try:
        async with asyncio.timeout(2):
            while "Page.startScreencast" not in sent:
                await asyncio.sleep(0.01)
            for number in range(3):
                handlers["Page.screencastFrame"]({"data": "AA==", "sessionId": number})
            await asyncio.sleep(0.1)
            assert "Page.screencastFrameAck" not in sent
            assert sent.count("Page.startScreencast") == 1
            assert await feed.after(0) == (b"\x00", "tab", 3)
            while sent.count("Page.startScreencast") < 2:
                await asyncio.sleep(0.01)
        assert sent.count("Page.startScreencast") == 2
        assert "Page.screencastFrameAck" not in sent
    finally:
        cast.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cast
