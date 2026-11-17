"""A process that holds one macOS or Windows virtual display.

A ``CGVirtualDisplay`` exists only while the process that created it runs,
and a claimed Windows monitor stays claimed only while its lock is held. A
persistent browser session outlives the command that opened it, so the
display lives in this small helper, which the opener starts with
:func:`start` and anyone stops with :func:`stop_detached`.

The helper holds an exclusive lock on ``lock`` in its directory for its whole
life, so a stopper can tell it is gone without trusting a PID. It writes
``display.json`` once the display is ready and releases the display when
``run`` is deleted, when it gets SIGTERM, or, unless detached, when its owner
closes its stdin (including by dying).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess  # nosec B404  # noqa: S404 - fixed argv: this interpreter and this module
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from pathlib import Path

from webskrap.display._xvfb import DIRECTORY_PREFIX, STOP_TIMEOUT_S
from webskrap.errors import ErrorCode, WebSkrapError

START_TIMEOUT_S = 30.0
READY_FILE = "display.json"
RUN_FILE = "run"
LOCK_FILE = "lock"
ERROR_FILE = "error.txt"
LOG_FILE = "helper.log"
_POLL_S = 0.05

Backend = Callable[[int, int], AbstractContextManager[tuple[int, int]]]


def start(
    width: int, height: int, *, detached: bool
) -> tuple[subprocess.Popen[bytes], Path, tuple[int, int]]:
    """Start a helper and wait until its display is ready.

    Returns the helper process, its owner-only directory and the display's
    top-left corner in desktop coordinates.

    Raises:
        WebSkrapError: ``browser_launch`` with the backend's reason if the
            display cannot be created, or if it is not ready in time.
    """
    directory = Path(tempfile.mkdtemp(prefix=DIRECTORY_PREFIX))
    try:
        (directory / RUN_FILE).touch()
        command = [sys.executable, "-P", "-m", "webskrap.display._helper"]
        command += [str(directory), str(int(width)), str(int(height))]
        if detached:
            command.append("--detached")
        with (directory / LOG_FILE).open("wb") as log:
            process = subprocess.Popen(  # nosec B603  # noqa: S603
                command,
                stdin=subprocess.DEVNULL if detached else subprocess.PIPE,
                stdout=log,
                stderr=log,
                cwd=directory,
                start_new_session=sys.platform != "win32",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
                ),
            )
        try:
            origin = _wait_until_ready(process, directory)
        except BaseException:
            _end(process, directory)
            raise
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return process, directory, origin


def _wait_until_ready(process: subprocess.Popen[bytes], directory: Path) -> tuple[int, int]:
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        with suppress(FileNotFoundError, PermissionError, ValueError, KeyError, TypeError):
            ready = json.loads((directory / READY_FILE).read_text(encoding="utf-8"))
            return int(ready["x"]), int(ready["y"])
        if process.poll() is not None:
            raise WebSkrapError(_failure(directory, process), code=ErrorCode.BROWSER_LAUNCH)
        time.sleep(_POLL_S)
    msg = f"the virtual display was not ready within {START_TIMEOUT_S:.0f}s"
    raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)


def _failure(directory: Path, process: subprocess.Popen[bytes]) -> str:
    for name in (ERROR_FILE, LOG_FILE):
        with suppress(OSError):
            text = (directory / name).read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text.splitlines()[-1][:500]
    return f"the virtual display helper exited (status {process.returncode})"


def stop(process: subprocess.Popen[bytes], directory: Path) -> None:
    """Ask the helper to release its display, wait for it, and delete its directory."""
    _end(process, directory)
    _remove_directory(directory)


def _end(process: subprocess.Popen[bytes], directory: Path) -> None:
    (directory / RUN_FILE).unlink(missing_ok=True)
    if process.stdin is not None:
        with suppress(OSError):
            process.stdin.close()
    try:
        process.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def is_helper_directory(directory: Path) -> bool:
    """True when ``directory`` belongs to a helper rather than to Xvfb."""
    return (directory / LOCK_FILE).exists()


def stop_detached(pid: int, directory: Path) -> None:
    """Stop the helper of ``directory`` from any process, then delete the directory.

    ``pid`` is signalled only while the lock is held and the process identity
    matches this directory (or its recorded Windows creation time).
    """
    (directory / RUN_FILE).unlink(missing_ok=True)
    lock = directory / LOCK_FILE
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while _lock_held(lock) and time.monotonic() < deadline:
        _reap(pid)
        time.sleep(_POLL_S)
    if _lock_held(lock) and _is_helper(pid, directory):
        with suppress(OSError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + STOP_TIMEOUT_S
        while _lock_held(lock) and time.monotonic() < deadline:
            _reap(pid)
            time.sleep(_POLL_S)
    if _lock_held(lock) and sys.platform != "win32" and _is_helper(pid, directory):
        with suppress(OSError):
            os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + STOP_TIMEOUT_S
        while _lock_held(lock) and time.monotonic() < deadline:
            _reap(pid)
            time.sleep(_POLL_S)
    _reap(pid)
    if _lock_held(lock):
        msg = f"virtual display helper did not stop; recovery files kept at {directory}"
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
    _remove_directory(directory)


def _remove_directory(directory: Path) -> None:
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while True:
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_POLL_S)
        else:
            return


def _is_helper(pid: int, directory: Path) -> bool:
    if sys.platform == "win32":
        from webskrap.browser_session import _windows_process_start

        with suppress(OSError, ValueError, KeyError, TypeError):
            ready = json.loads((directory / READY_FILE).read_text(encoding="utf-8"))
            started = _windows_process_start(pid)
            return started is not None and started.isoformat() == ready["created_at"]
        return False
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        result = subprocess.run(  # nosec B603  # noqa: S603
            ["/bin/ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            check=False,
        )
        command = result.stdout if result.returncode == 0 else b""
    return str(directory).encode() in command


def _reap(pid: int) -> None:
    if sys.platform != "win32":
        with suppress(ChildProcessError, OSError):
            os.waitpid(pid, os.WNOHANG)


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _lock_held(path: Path) -> bool:
    """True while another process holds the lock at ``path``."""
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        return not _try_lock(fd)
    finally:
        os.close(fd)


@contextmanager
def _hold_lock(path: Path) -> Iterator[None]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _try_lock(fd):
            msg = "another helper already holds this display directory"
            raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
        yield
    finally:
        os.close(fd)


def _backend() -> Backend:
    if sys.platform == "darwin":
        from webskrap.display import _macos

        return _macos.hold
    if sys.platform == "win32":
        from webskrap.display import _windows

        return _windows.hold
    msg = "the virtual display helper runs on macOS and Windows only"
    raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)


def _stop_requested(directory: Path, owner_gone: threading.Event) -> bool:
    return owner_gone.is_set() or not (directory / RUN_FILE).exists()


def _watch_owner(owner_gone: threading.Event) -> None:
    with suppress(OSError, ValueError):
        while sys.stdin.buffer.read(4096):
            pass
    owner_gone.set()


def run(
    directory: Path, width: int, height: int, *, detached: bool, backend: Backend | None = None
) -> int:
    """Hold a display until a stop is requested; return the exit status.

    ``backend`` defaults to this platform's. A failure is written to
    ``error.txt`` for the opener to report.
    """
    owner_gone = threading.Event()
    if not detached:
        threading.Thread(target=_watch_owner, args=(owner_gone,), daemon=True).start()
    try:
        hold = backend or _backend()
        with _hold_lock(directory / LOCK_FILE), hold(width, height) as (x, y):
            ready = directory / f"{READY_FILE}.tmp"
            state: dict[str, int | str] = {"x": x, "y": y}
            if sys.platform == "win32":
                from webskrap.browser_session import _windows_process_start

                started = _windows_process_start(os.getpid())
                if started is not None:
                    state["created_at"] = started.isoformat()
            ready.write_text(json.dumps(state), encoding="utf-8")
            ready.replace(directory / READY_FILE)
            while not _stop_requested(directory, owner_gone):
                owner_gone.wait(0.2)
    except Exception as exc:
        with suppress(OSError):
            (directory / ERROR_FILE).write_text(str(exc), encoding="utf-8")
        return 1
    return 0


def main(argv: list[str]) -> int:
    """Run the helper from its command line: ``DIRECTORY WIDTH HEIGHT [--detached]``."""
    directory, width, height = Path(argv[0]), int(argv[1]), int(argv[2])
    if not directory.name.startswith(DIRECTORY_PREFIX):
        return 2
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    return run(directory, width, height, detached="--detached" in argv[3:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
