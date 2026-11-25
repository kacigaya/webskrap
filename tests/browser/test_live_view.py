from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
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
    connection = AsyncMock(spec=CDPConnection)
    clicks: list[object] = []
    frames = iter(("Zmlyc3Q=", "c2Vjb25k"))

    def send(method: str, params: object = None, **_: object) -> dict[str, object]:
        match method:
            case "Target.getTargets":
                return {"targetInfos": [{"type": "page", "targetId": "tab"}]}
            case "Target.attachToTarget":
                return {"sessionId": "attached"}
            case "Page.captureScreenshot":
                assert params == {"format": "jpeg", "quality": 90}
                return {"data": next(frames)}
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
    ) -> tuple[int, str | None, bytes]:
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
            return reply.status, reply.getheader("Cache-Control"), reply.read()
        finally:
            conn.close()

    try:
        status, cache, html = await asyncio.to_thread(request, "/")
        assert (status, cache) == (200, "no-store")
        assert b"WebSkrap Live View" in html
        assert address.fragment.encode() not in html
        assert (await asyncio.to_thread(request, "/frame", "wrong"))[0] == 404
        connection.send.assert_not_awaited()
        assert await asyncio.to_thread(request, "/frame", address.fragment) == (
            200,
            "no-store",
            b"first",
        )
        assert (await asyncio.to_thread(request, "/frame", address.fragment))[2] == b"second"
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
        assert (await asyncio.to_thread(request, "/frame", address.fragment))[0] == 410
        assert [call.args[0] for call in connection.send.await_args_list].count(
            "Page.captureScreenshot"
        ) == 2
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
