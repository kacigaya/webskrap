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


@pytest.mark.parametrize("allow_scroll", [False, True])
async def test_live_view_requires_token_and_stays_with_original_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allow_scroll: bool
) -> None:
    state = {"pid": 42, "port": 9222}
    monkeypatch.setattr(
        live_view.browser_session, "_open_state", lambda _: (tmp_path, state.copy())
    )
    connection = AsyncMock(spec=CDPConnection)
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
                assert params == {
                    "type": "mouseWheel",
                    "x": 25,
                    "y": 100,
                    "deltaX": 0,
                    "deltaY": 120,
                }
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
    url, task = await live_view.start("visual", allow_scroll=allow_scroll)
    address = urlsplit(url)
    assert address.hostname == "127.0.0.1"

    def request(path: str, token: str = "", method: str = "GET") -> tuple[int, str | None, bytes]:
        conn = HTTPConnection(address.hostname, address.port, timeout=2)
        try:
            conn.request(method, path, headers={"Authorization": f"Bearer {token}"})
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
            204 if allow_scroll else 404
        )
        if allow_scroll:
            bad = "/scroll?x=.25&y=.5&dx=0&dy=nan"
            assert (await asyncio.to_thread(request, bad, address.fragment, "POST"))[0] == 400
        state["pid"] += 1
        if allow_scroll:
            assert (await asyncio.to_thread(request, scroll, address.fragment, "POST"))[0] == 410
        assert [call.args[0] for call in connection.send.await_args_list].count(
            "Input.dispatchMouseEvent"
        ) == int(allow_scroll)
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
