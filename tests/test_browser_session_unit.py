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
        }
    ]


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
