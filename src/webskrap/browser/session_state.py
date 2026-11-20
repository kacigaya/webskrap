"""Persistent session paths, state files, and operation locks."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import time
from pathlib import Path
from typing import Any, BinaryIO

from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.security.paths import secure_directory

SESSION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class _SessionOperationLock:
    """Cross-process exclusive lock for one session's open/close operations."""

    def __init__(self, handle: BinaryIO) -> None:
        """Keep the locked file open until :meth:`release` is called."""
        self._handle = handle

    @classmethod
    def acquire(cls, name: str) -> _SessionOperationLock:
        """Block until this process exclusively owns ``name``'s operation lock."""
        # Validate before creating the lock filename. The session directory is
        # validated again inside the lock before either operation touches it.
        session_dir(name)
        root = sessions_root()
        secure_directory(root, tighten_existing=False)
        lock_directory = secure_directory(root / ".locks")
        handle = (lock_directory / f"{name}.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in {errno.EACCES, errno.EDEADLK}:
                            raise
                        # LK_LOCK gives up after ten one-second retries, but a
                        # valid Chromium launch may hold this lock for 20s.
                        time.sleep(0.05)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except BaseException:
            handle.close()
            raise
        return cls(handle)

    def release(self) -> None:
        """Release the operation lock and close its file handle."""
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()


async def _acquire_session_operation_lock(name: str) -> _SessionOperationLock:
    """Acquire a lock in a worker, releasing it if this task is cancelled."""
    acquire_task = asyncio.create_task(asyncio.to_thread(_SessionOperationLock.acquire, name))
    try:
        return await asyncio.shield(acquire_task)
    except BaseException:
        # Cancelling to_thread does not stop its worker. Wait for that worker to
        # acquire, then release, or it would orphan the lock until process exit.
        operation_lock = await acquire_task
        await asyncio.to_thread(operation_lock.release)
        raise


def sessions_root() -> Path:
    """Return the directory holding every persistent session's profile data.

    ``WEBSKRAP_BROWSER_DIR`` overrides the default ``~/.webskrap/browser``.
    """
    if override := os.environ.get("WEBSKRAP_BROWSER_DIR"):
        return Path(override).expanduser()
    return Path.home() / ".webskrap" / "browser"


def session_dir(name: str) -> Path:
    """Return a session's directory, rejecting names that escape the root.

    Raises:
        WebSkrapError: If ``name`` is ``.`` or ``..``, or contains characters
            other than letters, digits, ``.``, ``_`` or ``-``, or if its
            directory is a symlink.
    """
    # Session names become directory names, so reject path separators and
    # anything else that could escape the sessions root.
    if name in {".", ".."} or not SESSION_NAME_PATTERN.fullmatch(name):
        msg = (
            f"invalid session name '{name}': use letters, digits, '.', '_' or '-', "
            "but not '.' or '..'"
        )
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    directory = sessions_root() / name
    if directory.is_symlink():
        msg = f"session directory must not be a symlink: {directory}"
        raise WebSkrapError(msg, code=ErrorCode.PATH_REJECTED)
    return directory


def create_session_dir(name: str) -> Path:
    """Create a session's directory tree owner-only and return it.

    Profile data under it holds cookies and logged-in state, so the session
    directory is created (or tightened to) ``0700``. The sessions root is only
    tightened when WebSkrap creates it: a ``WEBSKRAP_BROWSER_DIR`` the user
    already set up may be shared on purpose, and the per-session directories
    inside it are what actually hold the cookies.
    """
    directory = session_dir(name)
    secure_directory(sessions_root(), tighten_existing=False)
    return secure_directory(directory)


def state_path(directory: Path) -> Path:
    """Return the path of a session directory's state file."""
    return directory / "state.json"


def read_state(directory: Path) -> dict[str, Any] | None:
    """Return a session's recorded state, or None when missing or unreadable."""
    try:
        state = json.loads(state_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or "pid" not in state or "port" not in state:
        return None
    return state


def write_state(directory: Path, state: dict[str, Any]) -> None:
    """Write a session's state file so readers never see a partial file."""
    # Atomic replace: a concurrent action must never read a half-written file.
    temp_path = state_path(directory).with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temp_path, state_path(directory))


def is_session_dir(path: Path) -> bool:
    """True when ``path`` looks like a session directory this tool manages."""
    if path.is_symlink():
        return False
    return (
        path.is_dir()
        and SESSION_NAME_PATTERN.fullmatch(path.name) is not None
        and (state_path(path).exists() or (path / "user-data").is_dir())
    )
