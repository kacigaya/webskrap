from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from webskrap import browser_session
from webskrap.client import WebSkrapError
from webskrap.errors import ErrorCode

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")


class _FakeProcess:
    """Stand-in for a launched browser that never reports a DevTools port."""

    pid = 4_242_424

    def __init__(self, exited: bool) -> None:
        self._exited = exited

    def poll(self) -> int | None:
        return 1 if self._exited else None


def _capture_launch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exited: bool = True,
    log_output: str = "",
) -> list[list[str]]:
    """Record the argv of each launch attempt instead of starting a browser.

    ``log_output`` is written where the real browser writes its own startup
    log, which ``launch_browser`` opens (and truncates) before spawning.
    """
    commands: list[list[str]] = []

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        commands.append(command)
        if log_output:
            kwargs["stdout"].write(log_output.encode())
            kwargs["stdout"].flush()
        return _FakeProcess(exited)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(browser_session, "signal_group", lambda *_args: None)
    monkeypatch.setattr(browser_session, "LAUNCH_TIMEOUT_S", 0.05)
    return commands


def _launch(tmp_path: Path, **kwargs: Any) -> None:
    with pytest.raises(WebSkrapError):
        browser_session.launch_browser(tmp_path, executable="/bin/chromium", **kwargs)


def test_launch_sandboxes_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    commands = _capture_launch(monkeypatch)

    _launch(tmp_path, headless=True)

    assert "--no-sandbox" not in commands[0]


def test_launch_opt_out_passes_no_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    commands = _capture_launch(monkeypatch)

    _launch(tmp_path, headless=True, chromium_sandbox=False)

    assert "--no-sandbox" in commands[0]


def test_launch_failure_does_not_retry_without_the_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = _capture_launch(monkeypatch)

    _launch(tmp_path, headless=True)

    assert len(commands) == 1


def test_launch_failure_suggests_the_sandbox_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _capture_launch(
        monkeypatch,
        log_output="Failed to move to new namespace: ... clone_newuser\n",
    )

    with pytest.raises(WebSkrapError, match="--no-sandbox"):
        browser_session.launch_browser(tmp_path, executable="/bin/chromium", headless=True)


def test_launch_failure_without_sandbox_evidence_stays_quiet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _capture_launch(monkeypatch, log_output="Segmentation fault\n")

    with pytest.raises(WebSkrapError) as excinfo:
        browser_session.launch_browser(tmp_path, executable="/bin/chromium", headless=True)

    assert "sandbox" not in str(excinfo.value)


def test_launch_timeout_reports_the_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _capture_launch(monkeypatch, exited=False)

    with pytest.raises(WebSkrapError, match="did not report a DevTools port"):
        browser_session.launch_browser(tmp_path, executable="/bin/chromium", headless=True)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("0", False, id="zero"),
        pytest.param("false", False, id="false"),
        pytest.param("No", False, id="no-mixed-case"),
        pytest.param(" off ", False, id="off-padded"),
        pytest.param("1", True, id="one"),
        pytest.param("true", True, id="true"),
        pytest.param("", True, id="empty-is-not-an-opt-out"),
        # Anything unrecognized keeps the sandbox: a typo must not silently
        # disable renderer isolation.
        pytest.param("maybe", True, id="unrecognized"),
        pytest.param("disabled", True, id="near-miss-word"),
        pytest.param("0 ", False, id="zero-trailing-space"),
    ],
)
def test_sandbox_env_opt_out(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv(browser_session.SANDBOX_ENV, value)

    assert browser_session.sandbox_enabled() is expected


def test_sandbox_defaults_on_without_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(browser_session.SANDBOX_ENV, raising=False)

    assert browser_session.sandbox_enabled() is True


@pytest.mark.parametrize("explicit", [True, False])
def test_explicit_argument_beats_the_env(monkeypatch: pytest.MonkeyPatch, explicit: bool) -> None:
    monkeypatch.setenv(browser_session.SANDBOX_ENV, "0" if explicit else "1")

    assert browser_session.sandbox_enabled(explicit) is explicit


@posix_only
def test_session_directories_are_owner_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "browser"
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(root))

    directory = browser_session.create_session_dir("default")

    assert directory == root / "default"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@posix_only
def test_profile_directory_is_owner_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _capture_launch(monkeypatch)

    _launch(tmp_path, headless=True)

    assert stat.S_IMODE((tmp_path / "user-data").stat().st_mode) == 0o700


@posix_only
def test_existing_sessions_root_keeps_its_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "browser"
    root.mkdir(mode=0o755)
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(root))

    directory = browser_session.create_session_dir("default")

    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@posix_only
def test_existing_session_dir_is_tightened(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "browser"
    (root / "default").mkdir(parents=True, mode=0o755)
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(root))

    directory = browser_session.create_session_dir("default")

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_list_sessions_reports_sandbox_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path))
    directory = tmp_path / "default"
    directory.mkdir()
    browser_session.write_state(directory, {"pid": 2**22 - 1, "port": 1, "chromium_sandbox": True})

    # The PID is dead, so the session reports as stopped and its launch
    # details, sandbox included, are unknown rather than stale.
    assert browser_session.list_sessions() == [
        {
            "session": "default",
            "running": False,
            "pid": None,
            "port": None,
            "chromium_sandbox": None,
            "proxy_server": None,
        }
    ]


def test_list_sessions_reports_the_running_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path))
    directory = tmp_path / "proxied"
    directory.mkdir()
    browser_session.write_state(
        directory,
        {"pid": 7, "port": 1, "chromium_sandbox": True, "proxy_server": "http://proxy.test:8080"},
    )
    monkeypatch.setattr(browser_session, "session_running", lambda _d, state: state is not None)

    [entry] = browser_session.list_sessions()

    assert entry["proxy_server"] == "http://proxy.test:8080"


@pytest.mark.parametrize("name", [".", "..", "../evil"])
def test_create_session_dir_rejects_traversal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path / "browser"))

    with pytest.raises(WebSkrapError, match="invalid session name"):
        browser_session.create_session_dir(name)

    assert not (tmp_path / "browser").exists()
    assert not (tmp_path / "evil").exists()


def test_session_dir_rejects_symlink_without_touching_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "browser"
    target = tmp_path / "target"
    root.mkdir()
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    (root / "default").symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(root))

    with pytest.raises(WebSkrapError, match="session directory must not be a symlink"):
        browser_session.create_session_dir("default")
    with pytest.raises(WebSkrapError, match="session directory must not be a symlink"):
        browser_session.close_session("default", delete_data=True)

    assert marker.read_text(encoding="utf-8") == "keep"


def test_session_operation_lock_serializes_same_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path / "browser"))
    first = browser_session._SessionOperationLock.acquire("default")
    waiting = threading.Event()
    acquired = threading.Event()

    def acquire_second() -> None:
        waiting.set()
        second = browser_session._SessionOperationLock.acquire("default")
        acquired.set()
        second.release()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    assert waiting.wait(timeout=1)
    assert not acquired.wait(timeout=0.05)

    first.release()
    assert acquired.wait(timeout=1)
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_concurrent_open_reuses_single_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path / "browser"))
    launches: list[Path] = []

    async def exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        executable_requested = asyncio.Event()
        continue_launch = asyncio.Event()

        async def fake_executable() -> str:
            executable_requested.set()
            await continue_launch.wait()
            return "/bin/chromium"

        def fake_launch(directory: Path, **_kwargs: Any) -> tuple[int, int]:
            launches.append(directory)
            return (1234, 5678)

        monkeypatch.setattr(browser_session, "chromium_executable", fake_executable)
        monkeypatch.setattr(browser_session, "launch_browser", fake_launch)
        monkeypatch.setattr(
            browser_session,
            "session_running",
            lambda _directory, state: state is not None,
        )

        first = asyncio.create_task(browser_session.open_session("default"))
        await executable_requested.wait()
        second = asyncio.create_task(browser_session.open_session("default"))
        await asyncio.sleep(0)
        continue_launch.set()
        return await first, await second

    first, second = asyncio.run(exercise())

    assert launches == [tmp_path / "browser" / "default"]
    assert first["reused"] is False
    assert second["reused"] is True


def test_cancelled_lock_wait_releases_after_acquiring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path / "browser"))
    first = browser_session._SessionOperationLock.acquire("default")
    original_acquire = browser_session._SessionOperationLock.acquire
    waiting = threading.Event()

    def observed_acquire(name: str) -> browser_session._SessionOperationLock:
        waiting.set()
        return original_acquire(name)

    monkeypatch.setattr(browser_session._SessionOperationLock, "acquire", observed_acquire)

    async def exercise() -> None:
        task = asyncio.create_task(browser_session._acquire_session_operation_lock("default"))
        assert await asyncio.to_thread(waiting.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        first.release()
        with pytest.raises(asyncio.CancelledError):
            await task

        reacquired = await asyncio.to_thread(original_acquire, "default")
        reacquired.release()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({}, id="none"),
        pytest.param({"text": "a", "selector": "b"}, id="two"),
        pytest.param({"text": "a", "text_gone": "b", "load_state": "load"}, id="three"),
    ],
)
def test_wait_condition_requires_exactly_one(kwargs: dict[str, str]) -> None:
    with pytest.raises(WebSkrapError, match="exactly one of") as caught:
        browser_session.wait_condition(
            kwargs.get("text"),
            kwargs.get("text_gone"),
            kwargs.get("selector"),
            kwargs.get("load_state"),
        )

    assert caught.value.code is ErrorCode.USAGE


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param({"text": "Done"}, "text", id="text"),
        pytest.param({"text_gone": "Loading"}, "text_gone", id="text-gone"),
        pytest.param({"selector": "e12"}, "selector", id="selector"),
        pytest.param({"load_state": "networkidle"}, "load_state", id="load-state"),
    ],
)
def test_wait_condition_names_the_single_condition(kwargs: dict[str, str], expected: str) -> None:
    assert (
        browser_session.wait_condition(
            kwargs.get("text"),
            kwargs.get("text_gone"),
            kwargs.get("selector"),
            kwargs.get("load_state"),
        )
        == expected
    )


def test_shape_snapshot_pages_through_the_tree() -> None:
    payload = {"url": "https://example.test", "title": "t", "snapshot": "0123456789"}

    first = browser_session.shape_snapshot(payload, 4)
    assert first["snapshot"] == "0123"
    assert first["snapshot_length"] == 10
    assert first["snapshot_truncated"] is True
    assert first["next_snapshot_offset"] == 4
    # The page state travels with every window, not just the first.
    assert first["title"] == "t"

    last = browser_session.shape_snapshot(payload, 100, 4)
    assert last["snapshot"] == "456789"
    assert last["snapshot_truncated"] is False
    assert last["next_snapshot_offset"] is None


def test_shape_eval_result_returns_small_values_untouched() -> None:
    assert browser_session.shape_eval_result({"a": 1}, 100) == {
        "result": {"a": 1},
        "result_length": 8,
        "result_truncated": False,
    }


def test_shape_eval_result_clips_a_large_value_to_encoded_json() -> None:
    payload = browser_session.shape_eval_result("x" * 500, 20)

    assert payload["result"] is None
    assert payload["result_truncated"] is True
    assert payload["result_length"] == 502
    assert payload["result_json"] == '"' + "x" * 19


def test_shape_eval_result_distinguishes_null_from_truncation() -> None:
    # A null result and a clipped one both carry result=None, so only
    # result_truncated tells them apart.
    assert browser_session.shape_eval_result(None, 100)["result_truncated"] is False


def test_shape_eval_result_encodes_values_json_cannot_represent() -> None:
    payload = browser_session.shape_eval_result({"n": float("inf")}, 100)

    assert payload["result_truncated"] is False


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("file:///etc/passwd", id="file"),
        pytest.param("ftp://example.test/x", id="ftp"),
        pytest.param("https://user:pass@example.test/", id="userinfo"),
    ],
)
def test_goto_rejects_unfetchable_targets(url: str) -> None:
    with pytest.raises(WebSkrapError) as caught:
        asyncio.run(browser_session.goto(None, url, "load"))  # type: ignore[arg-type]

    assert caught.value.code is ErrorCode.USAGE


def test_is_session_dir_rejects_a_symlink(tmp_path: Path) -> None:
    session = tmp_path / "shop"
    (session / "user-data").mkdir(parents=True)
    link = tmp_path / "shoplink"
    link.symlink_to(session, target_is_directory=True)

    assert browser_session.is_session_dir(session) is True
    assert browser_session.is_session_dir(link) is False


def test_sessions_root_expands_a_tilde_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", "~/sessions")

    assert str(browser_session.sessions_root()).endswith("sessions")
    assert "~" not in str(browser_session.sessions_root())


def test_launch_hides_automation_and_sets_a_virtual_screen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A remote-debugging port alone sets navigator.webdriver, and headless
    # Chrome otherwise reports an 800x600 screen with the window at 10,10.
    commands = _capture_launch(monkeypatch)

    _launch(tmp_path, headless=True)

    command = commands[0]
    assert "--disable-blink-features=AutomationControlled" in command
    assert "--screen-info={1920x1080}" in command
    assert "--window-size=1840,1000" in command
    assert "--window-position=0,0" in command
    assert command.count("--no-sandbox") == 0


def test_headed_launch_keeps_the_real_display(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = _capture_launch(monkeypatch)

    _launch(tmp_path, headless=False)

    command = commands[0]
    assert "--disable-blink-features=AutomationControlled" in command
    assert not any(arg.startswith(("--screen-info", "--window-size")) for arg in command)
    assert "--headless=new" not in command


def test_launch_opt_out_adds_no_sandbox_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = _capture_launch(monkeypatch)

    _launch(tmp_path, headless=True, chromium_sandbox=False)

    assert commands[0].count("--no-sandbox") == 1


async def test_evaluate_runs_in_the_page_world() -> None:
    # Patchright evaluates in an isolated world by default, which hides page
    # globals from `browser eval`.
    calls: list[tuple[str, dict[str, Any]]] = []

    class _Page:
        async def evaluate(self, expression: str, **kwargs: Any) -> str:
            calls.append((expression, kwargs))
            return "ok"

    result = await browser_session.evaluate(_Page(), "window.app")  # type: ignore[arg-type]

    assert result == "ok"
    assert calls == [("window.app", {"isolated_context": False})]


def test_launch_routes_through_the_proxy_and_blocks_webrtc_leaks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = _capture_launch(monkeypatch)

    _launch(tmp_path, headless=True, proxy_server="socks5://proxy.test:1080")

    command = commands[0]
    assert "--proxy-server=socks5://proxy.test:1080" in command
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in command
    assert "--force-webrtc-ip-handling-policy" in command


def test_launch_without_proxy_leaves_webrtc_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = _capture_launch(monkeypatch)

    _launch(tmp_path, headless=True)

    assert not any(a.startswith(("--proxy-server", "--webrtc")) for a in commands[0])


def test_launch_explicit_webrtc_policy_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = _capture_launch(monkeypatch)

    _launch(
        tmp_path,
        headless=True,
        proxy_server="http://proxy.test:8080",
        webrtc_ip_handling_policy="default_public_interface_only",
    )

    assert "--webrtc-ip-handling-policy=default_public_interface_only" in commands[0]


@pytest.mark.parametrize(
    ("server", "message"),
    [
        pytest.param("proxy.test:8080", "must start with", id="no-scheme"),
        pytest.param("ftp://proxy.test", "must start with", id="bad-scheme"),
        pytest.param("http://user:secret@proxy.test:8080", "cannot authenticate", id="credentials"),
        pytest.param("socks5://user@proxy.test:1080", "cannot authenticate", id="username-only"),
    ],
)
def test_persistent_proxy_is_validated(server: str, message: str) -> None:
    with pytest.raises(WebSkrapError, match=message) as excinfo:
        browser_session.persistent_proxy_server(server)

    assert excinfo.value.code is ErrorCode.USAGE
    # A refused credential never reaches the error text.
    assert "secret" not in str(excinfo.value)


def test_persistent_proxy_accepts_paths_with_at_signs_outside_the_authority() -> None:
    server = "http://proxy.test:8080/p@th"

    assert browser_session.persistent_proxy_server(server) == server


def _stub_session_launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    """Make open_session "launch" instantly, recording each launch's options."""
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path / "browser"))
    launches: list[dict[str, Any]] = []

    async def fake_executable() -> str:
        return "/bin/chromium"

    def fake_launch(_directory: Path, **kwargs: Any) -> tuple[int, int]:
        launches.append(kwargs)
        return (1234, 5678)

    monkeypatch.setattr(browser_session, "chromium_executable", fake_executable)
    monkeypatch.setattr(browser_session, "launch_browser", fake_launch)
    monkeypatch.setattr(
        browser_session, "session_running", lambda _directory, state: state is not None
    )
    return launches


def test_open_records_the_proxy_and_its_webrtc_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launches = _stub_session_launch(monkeypatch, tmp_path)

    opened = asyncio.run(
        browser_session.open_session("proxied", proxy_server="socks5://proxy.test:1080")
    )

    assert launches[0]["proxy_server"] == "socks5://proxy.test:1080"
    assert opened["proxy_server"] == "socks5://proxy.test:1080"
    assert opened["webrtc_ip_handling_policy"] == "disable_non_proxied_udp"


def test_open_without_proxy_records_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_session_launch(monkeypatch, tmp_path)

    opened = asyncio.run(browser_session.open_session("direct"))

    assert opened["proxy_server"] is None
    assert opened["webrtc_ip_handling_policy"] is None


@pytest.mark.parametrize(
    ("first", "second", "label"),
    [
        pytest.param({}, {"proxy_server": "http://proxy.test:8080"}, "proxy", id="adds-proxy"),
        pytest.param(
            {"proxy_server": "http://a.test:8080"},
            {"proxy_server": "http://b.test:8080"},
            "proxy",
            id="changes-proxy",
        ),
        pytest.param(
            {"proxy_server": "http://a.test:8080"},
            {"webrtc_ip_handling_policy": "default"},
            "WebRTC policy",
            id="changes-webrtc",
        ),
    ],
)
def test_reopen_refuses_a_different_network_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    first: dict[str, Any],
    second: dict[str, Any],
    label: str,
) -> None:
    # Reusing the running browser would send traffic outside the requested proxy.
    launches = _stub_session_launch(monkeypatch, tmp_path)
    asyncio.run(browser_session.open_session("net", **first))

    with pytest.raises(WebSkrapError, match=f"different {label}") as excinfo:
        asyncio.run(browser_session.open_session("net", **second))

    assert excinfo.value.code is ErrorCode.USAGE
    assert len(launches) == 1


def test_reopen_without_naming_the_proxy_reuses_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_session_launch(monkeypatch, tmp_path)
    asyncio.run(browser_session.open_session("net", proxy_server="http://proxy.test:8080"))

    reopened = asyncio.run(browser_session.open_session("net"))

    assert reopened["reused"] is True
    assert reopened["proxy_server"] == "http://proxy.test:8080"


def test_open_rejects_a_credentialed_proxy_before_launching(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launches = _stub_session_launch(monkeypatch, tmp_path)

    with pytest.raises(WebSkrapError, match="cannot authenticate"):
        asyncio.run(browser_session.open_session("net", proxy_server="http://u:p@proxy.test:8080"))

    assert launches == []


class _ActionLocator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def click(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("click", args, kwargs))

    async def fill(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("fill", args, kwargs))


def _action_page(locator: _ActionLocator) -> Any:
    class _PageStub:
        def locator(self, _selector: str) -> _ActionLocator:
            return locator

    return _PageStub()


@pytest.mark.parametrize(("action", "extra"), [("click", {}), ("dblclick", {"click_count": 2})])
def test_click_actions_use_the_human_path(
    monkeypatch: pytest.MonkeyPatch, action: str, extra: dict[str, int]
) -> None:
    human_clicks: list[dict[str, Any]] = []

    async def fake_human_click(_page: Any, _locator: Any, **options: Any) -> None:
        human_clicks.append(options)

    monkeypatch.setattr("webskrap.human.click", fake_human_click)
    locator = _ActionLocator()

    asyncio.run(browser_session.element_action(_action_page(locator), action, "#go", []))

    # No Playwright click at all, not even a trial one: the page would see it.
    assert locator.calls == []
    assert human_clicks == [{"description": "#go", **extra}]


def test_fill_still_sets_the_value_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    locator = _ActionLocator()

    asyncio.run(browser_session.element_action(_action_page(locator), "fill", "#q", ["hi"]))

    assert locator.calls == [("fill", ("hi",), {})]


def test_type_action_uses_human_typing(monkeypatch: pytest.MonkeyPatch) -> None:
    typed: list[tuple[str, dict[str, Any]]] = []

    async def fake_type_text(_page: Any, _locator: Any, text: str, **options: Any) -> None:
        typed.append((text, options))

    monkeypatch.setattr("webskrap.human.type_text", fake_type_text)
    locator = _ActionLocator()

    asyncio.run(browser_session.element_action(_action_page(locator), "type", "#q", ["hello"]))

    assert typed == [("hello", {"description": "#q"})]
    assert locator.calls == []  # no press_sequentially
