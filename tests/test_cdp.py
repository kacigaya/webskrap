"""Unit tests for WebSkrap's own CDP driver core and fetch flag assembly."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from webskrap import cli
from webskrap.cdp import (
    _EVALUATE_SCRIPT,
    CDPConnection,
    CDPError,
    CDPPage,
    EventRecorder,
    PipeTransport,
    find_executable,
)
from webskrap.cdp_fetch import launch_args
from webskrap.errors import ErrorCode, WebSkrapError, is_launch_failure
from webskrap.models import FetchResult, ProxyConfig, SessionConfig
from webskrap.profiles import get_profile


class _Writer:
    """Write transport stand-in that records every message sent."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        assert data.endswith(b"\0")
        self.sent.append(json.loads(data[:-1]))

    def close(self) -> None:
        self.closed = True


def _connection() -> tuple[CDPConnection, asyncio.StreamReader, _Writer]:
    reader = asyncio.StreamReader()
    writer = _Writer()
    connection = CDPConnection(PipeTransport(reader, writer))  # type: ignore[arg-type]
    connection.start()
    return connection, reader, writer


def _feed(reader: asyncio.StreamReader, message: dict[str, Any]) -> None:
    reader.feed_data(json.dumps(message).encode() + b"\0")


async def _reply_when_sent(
    reader: asyncio.StreamReader, writer: _Writer, index: int, reply: dict[str, Any]
) -> None:
    while len(writer.sent) <= index:
        await asyncio.sleep(0)
    _feed(reader, {"id": writer.sent[index]["id"], **reply})


async def test_send_resolves_the_matching_reply_and_routes_session_ids() -> None:
    connection, reader, writer = _connection()
    task = asyncio.create_task(connection.send("Page.navigate", {"url": "x"}, session_id="S1"))
    await _reply_when_sent(reader, writer, 0, {"result": {"frameId": "F"}})

    assert await task == {"frameId": "F"}
    assert writer.sent[0]["method"] == "Page.navigate"
    assert writer.sent[0]["sessionId"] == "S1"
    await connection.close()
    assert writer.closed


async def test_error_replies_raise_cdp_errors() -> None:
    connection, reader, writer = _connection()
    task = asyncio.create_task(connection.send("Bogus.method"))
    await _reply_when_sent(reader, writer, 0, {"error": {"code": -32601, "message": "not found"}})

    with pytest.raises(CDPError, match="not found"):
        await task
    await connection.close()


async def test_events_reach_only_their_session_handlers() -> None:
    connection, reader, _writer = _connection()
    page_events: list[dict[str, Any]] = []
    connection.on("Page.loadEventFired", page_events.append, session_id="S1")
    _feed(reader, {"method": "Page.loadEventFired", "params": {"n": 1}, "sessionId": "S2"})
    _feed(reader, {"method": "Page.loadEventFired", "params": {"n": 2}, "sessionId": "S1"})
    await asyncio.sleep(0.01)

    assert page_events == [{"n": 2}]
    await connection.close()


async def test_a_closed_pipe_fails_pending_and_later_commands() -> None:
    connection, reader, writer = _connection()
    connection.on_disconnect = lambda: WebSkrapError("gone", code=ErrorCode.BROWSER_LAUNCH)
    task = asyncio.create_task(connection.send("Browser.getVersion"))
    while not writer.sent:
        await asyncio.sleep(0)
    reader.feed_eof()

    with pytest.raises(WebSkrapError, match="gone"):
        await task
    with pytest.raises(WebSkrapError, match="gone"):
        await connection.send("Browser.getVersion")
    await connection.close()


async def test_send_timeout_is_a_timeout_error() -> None:
    connection, _reader, _writer = _connection()
    with pytest.raises(WebSkrapError) as error:
        await connection.send("Page.enable", timeout_s=0.01)
    assert error.value.code is ErrorCode.TIMEOUT
    await connection.close()


async def test_recorder_keeps_events_that_fire_before_the_wait() -> None:
    connection, reader, _writer = _connection()
    recorder = EventRecorder(connection, ["Page.lifecycleEvent"], session_id="S1")
    _feed(
        reader,
        {"method": "Page.lifecycleEvent", "params": {"name": "load"}, "sessionId": "S1"},
    )
    await asyncio.sleep(0.01)

    params = await recorder.wait(
        lambda _method, params: params["name"] == "load", timeout_s=1, what="load"
    )
    assert params == {"name": "load"}
    with pytest.raises(WebSkrapError) as error:
        await recorder.wait(lambda *_: False, timeout_s=0.01, what="never")
    assert error.value.code is ErrorCode.TIMEOUT
    recorder.stop()
    await connection.close()


class _FakeConnection:
    """Answers isolated-world and evaluate calls for :class:`CDPPage` tests."""

    def __init__(self, value: Any = None, exception: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.value = value
        self.exception = exception

    async def send(self, method: str, params: Any = None, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, dict(params or {})))
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if self.exception:
            return {"exceptionDetails": {"exception": {"description": self.exception}}}
        return {"result": {"value": self.value}}


async def test_evaluate_runs_in_an_isolated_world_without_runtime_enable() -> None:
    connection = _FakeConnection(value="ok")
    page = CDPPage(connection, "S1", "T1")  # type: ignore[arg-type]

    assert await page.evaluate("(max) => max + 1", 3) == "ok"
    world, evaluate = connection.calls
    assert world[0] == "Page.createIsolatedWorld"
    assert world[1]["frameId"] == "T1"
    assert evaluate[0] == "Runtime.evaluate"
    # Source and argument travel as JSON literals, never spliced into code.
    assert evaluate[1]["expression"] == f'({_EVALUATE_SCRIPT})("(max) => max + 1", 3)'
    assert evaluate[1]["contextId"] == 7
    # The stealth contract: page scripts never need the Runtime domain enabled.
    assert all(method != "Runtime.enable" for method, _ in connection.calls)


async def test_evaluate_raises_on_page_exceptions() -> None:
    page = CDPPage(_FakeConnection(exception="TypeError: boom"), "S1", "T1")  # type: ignore[arg-type]
    with pytest.raises(CDPError, match="boom"):
        await page.evaluate("() => null.x")


def _fake_chromium(root: Path, revision: int) -> Path:
    binary = root / f"chromium-{revision}" / "chrome-linux" / "chrome"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    return binary


def test_find_executable_picks_the_newest_downloaded_chromium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    _fake_chromium(tmp_path, 999)
    newest = _fake_chromium(tmp_path, 1243)

    assert find_executable(None) == newest
    assert find_executable("chromium") == newest


def test_find_executable_reads_package_local_browsers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PLAYWRIGHT_BROWSERS_PATH=0 keeps browsers inside the driver package.
    package = tmp_path / "playwright"
    package.mkdir()
    (package / "__init__.py").write_text("")
    binary = _fake_chromium(package / "driver" / "package" / ".local-browsers", 1243)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "playwright", raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")

    assert find_executable("chromium") == binary


def test_find_executable_errors_trigger_channel_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    with pytest.raises(WebSkrapError) as missing:
        find_executable("chromium")
    assert missing.value.code is ErrorCode.BROWSER_LAUNCH
    assert is_launch_failure(missing.value)

    with pytest.raises(WebSkrapError) as unknown:
        find_executable("firefox")
    assert unknown.value.code is ErrorCode.USAGE


def test_launch_args_share_patchright_flags_and_hide_webdriver() -> None:
    config = SessionConfig(
        chromium_sandbox=False,
        proxy=ProxyConfig(server="http://proxy.test:8080", bypass="localhost"),
        ignore_https_errors=True,
    )
    args, env = launch_args(config, get_profile(None))

    assert args[0] == "--disable-blink-features=AutomationControlled"
    assert "--no-sandbox" in args
    assert "--window-size=1840,1000" in args
    assert "--proxy-server=http://proxy.test:8080" in args
    assert "--proxy-bypass-list=localhost" in args
    assert "--ignore-certificate-errors" in args
    assert not any(a.startswith("--remote-debugging") for a in args)
    assert env == {}


@pytest.mark.parametrize(
    "config",
    [
        SessionConfig(decline_cookies=True),
        SessionConfig(mask_headless_user_agent=True),
        SessionConfig(java_script_enabled=False),
        SessionConfig(storage_state={"cookies": []}),
        SessionConfig(patchright_context_profile=True),
        SessionConfig(proxy=ProxyConfig(server="http://p.test:1", username="u", password="p")),
    ],
)
def test_launch_args_reject_unsupported_options(config: SessionConfig) -> None:
    with pytest.raises(WebSkrapError) as error:
        launch_args(config, get_profile(None))
    assert error.value.code is ErrorCode.USAGE


def test_cli_driver_cdp_routes_to_the_cdp_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_fetch(url: str, **kwargs: Any) -> FetchResult:
        calls.append({"url": url, **kwargs})
        return FetchResult(
            url=url,
            final_url=url,
            status=200,
            ok=True,
            headers={},
            text="body",
            title="T",
            cookies=[],
            timings={"elapsed_ms": 1.0},
        )

    monkeypatch.setattr(cli, "cdp_fetch", fake_fetch)
    result = CliRunner().invoke(
        cli.app, ["fetch", "https://example.test", "--driver", "cdp", "--channel", "chromium"]
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["url"] == "https://example.test"
    # The CLI's default-on consent dismissal is dropped, not rejected.
    assert calls[0]["config"].decline_cookies is False
    assert calls[0]["config"].channel == "chromium"

    # An explicit request is kept, so the CDP driver refuses it as usage.
    explicit = CliRunner().invoke(
        cli.app, ["fetch", "https://example.test", "--driver", "cdp", "--decline-cookies"]
    )
    assert explicit.exit_code == 0, explicit.output
    assert calls[1]["config"].decline_cookies is True


async def test_a_failing_event_handler_does_not_stop_replies() -> None:
    connection, reader, writer = _connection()

    def broken(_params: dict[str, Any]) -> None:
        raise RuntimeError("handler bug")

    connection.on("Page.loadEventFired", broken)
    _feed(reader, {"method": "Page.loadEventFired", "params": {}})
    task = asyncio.create_task(connection.send("Browser.getVersion", timeout_s=1))
    await _reply_when_sent(reader, writer, 0, {"result": {"product": "Chrome"}})

    assert await task == {"product": "Chrome"}
    await connection.close()


def test_the_core_imports_without_fcntl() -> None:
    import webskrap.cdp as cdp_module

    # fcntl does not exist on Windows; importing the CLI must still work there.
    assert "fcntl" not in vars(cdp_module)


def test_cli_search_driver_cdp_routes_to_the_cdp_search(monkeypatch: pytest.MonkeyPatch) -> None:
    from webskrap.models import SearchEngine, SearchResult

    calls: list[dict[str, Any]] = []

    async def fake_search(query: str, **kwargs: Any) -> SearchResult:
        calls.append({"query": query, **kwargs})
        return SearchResult(
            query=query,
            engine=SearchEngine.BING,
            url="https://www.bing.com/search?q=x",
            final_url="https://www.bing.com/search?q=x",
            status=200,
            ok=True,
            hits=[],
            hits_total=0,
            timings={"elapsed_ms": 1.0},
        )

    monkeypatch.setattr(cli, "cdp_search", fake_search)
    result = CliRunner().invoke(cli.app, ["search", "x", "--driver", "cdp", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert calls[0]["query"] == "x"
    assert calls[0]["config"].decline_cookies is False


async def test_fetch_rejects_an_unknown_ready_state() -> None:
    from webskrap.cdp_fetch import fetch

    with pytest.raises(WebSkrapError) as error:
        await fetch("https://example.com", ready_selector="body", ready_state="hidden")  # type: ignore[arg-type]
    assert error.value.code is ErrorCode.USAGE
