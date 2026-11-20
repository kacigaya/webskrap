"""Check browser process identity and stop process trees."""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404  # noqa: S404 - fixed argv for Chromium, never a shell
import sys
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from webskrap.browser.session_state import (
    _acquire_session_operation_lock as _acquire_session_operation_lock,
)
from webskrap.driver.launch import kill_windows_process_tree

LAUNCH_TIMEOUT_S = 20.0


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


PROXY_AUTH_MODULE = "webskrap.browser.proxy_auth"


def session_running(directory: Path, state: dict[str, Any] | None) -> bool:
    """True when the recorded PID is alive and is this session's browser.

    PIDs get recycled, so before trusting (or killing) one, require its
    command line to reference this session's user-data directory.
    """
    if state is None:
        return False
    pid = state["pid"]
    if sys.platform == "win32":
        return _windows_session_running(pid, state.get("created_at"))
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        if Path("/proc/self").exists():
            return False  # Linux: the PID is gone.
        # No /proc (macOS, BSD): ask ps for the command line instead.
        command = _ps_command(pid)
        return command is not None and str(directory / "user-data").encode() in command
    except OSError:
        # No /proc (non-Linux): fall back to a liveness-only check.
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    return str(directory / "user-data").encode() in cmdline


def _ps_command(pid: int) -> bytes | None:
    """Return a process's full command line from ``ps``, or None if it is gone."""
    # Fixed argv, no shell; the PID is an int this module recorded.
    result = subprocess.run(  # nosec B603  # noqa: S603
        ["/bin/ps", "-ww", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _windows_session_running(pid: int, created_at: str | None) -> bool:
    """Windows form of :func:`session_running`, without ``/proc``.

    Reading another process's command line needs undocumented NT calls, so a
    recycled PID is detected by its start time instead: the session browser
    started at most ``LAUNCH_TIMEOUT_S`` before ``created_at`` was recorded.
    A state without ``created_at`` cannot be matched, so it never counts.
    """
    if created_at is None:
        return False
    started = _windows_process_start(pid)
    if started is None:
        return False
    try:
        recorded = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    # created_at is truncated to whole seconds, so allow one second after it.
    earliest = recorded - timedelta(seconds=LAUNCH_TIMEOUT_S + 5)
    return earliest <= started <= recorded + timedelta(seconds=1)


def _windows_process_start(pid: int) -> datetime | None:
    """Return when a live Windows process started, or None if it is gone."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        if exit_code.value != _STILL_ACTIVE:
            return None
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
    finally:
        kernel32.CloseHandle(handle)
    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
    # FILETIME counts 100 ns intervals since 1601-01-01 UTC.
    return datetime(1601, 1, 1, tzinfo=UTC) + timedelta(microseconds=ticks // 10)


def kill_browser(pid: int, *, force: bool) -> None:
    """Stop the browser and its child processes; a vanished browser is fine.

    On POSIX the browser is launched with ``start_new_session=True``, so its
    PID is the group leader. Signalling only the leader (e.g. SIGKILL) would
    orphan renderer children that keep holding the profile's SingletonLock.
    Windows has no process groups: ``taskkill /T`` stops the child tree, and
    ``/F`` is always needed because a headless browser has no window to close.
    """
    if sys.platform == "win32":
        kill_windows_process_tree(pid)
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        with suppress(ProcessLookupError):
            os.kill(pid, sig)


def proxy_auth_running(pid: int) -> bool:
    """True when ``pid`` is a live proxy authentication helper."""
    if sys.platform == "win32":
        # No command line to check; a recycled PID only skips a warning.
        return _windows_process_start(pid) is not None
    try:
        command: bytes | None = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        command = None if Path("/proc/self").exists() else _ps_command(pid)
    return command is not None and (
        PROXY_AUTH_MODULE.encode() in command or b"webskrap.proxy_auth" in command
    )
