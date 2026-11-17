"""The Linux virtual display backend: a private Xvfb server.

The server is private to the session: it listens on no TCP port and requires
an MIT-MAGIC-COOKIE held in an owner-only file, so another local user cannot
connect to screenshot the page or inject input.
"""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import struct
import subprocess  # nosec B404  # noqa: S404 - fixed argv for Xvfb, never a shell
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from webskrap.errors import ErrorCode, WebSkrapError

START_TIMEOUT_S = 10.0
STOP_TIMEOUT_S = 5.0
DIRECTORY_PREFIX = "webskrap-display-"
# Xauthority family that matches any host; with an empty display number the
# entry matches whichever display Xvfb picks, so the file can be written
# before the server starts.
_FAMILY_WILD = 0xFFFF


def _xauth_field(value: bytes) -> bytes:
    return struct.pack(">H", len(value)) + value


def write_cookie_file(path: Path, cookie: bytes) -> None:
    """Write a one-entry Xauthority file readable only by its owner."""
    entry = (
        struct.pack(">H", _FAMILY_WILD)
        + _xauth_field(b"")
        + _xauth_field(b"")
        + _xauth_field(b"MIT-MAGIC-COOKIE-1")
        + _xauth_field(cookie)
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(entry)


def start(width: int, height: int) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Start a private Xvfb with one ``width`` x ``height`` 24-bit screen.

    Returns the server process, its ``DISPLAY`` value and its owner-only
    cookie directory.

    Raises:
        WebSkrapError: If Xvfb is not installed, or the server exits or does
            not become ready in time.
    """
    executable = shutil.which("Xvfb")
    if executable is None:
        msg = "virtual_display needs Xvfb. Install it (Debian/Ubuntu: apt install xvfb)"
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)

    directory = Path(tempfile.mkdtemp(prefix=DIRECTORY_PREFIX))
    try:
        auth_path = directory / "Xauthority"
        write_cookie_file(auth_path, secrets.token_bytes(16))
        read_fd, write_fd = os.pipe()
        try:
            with (directory / "xvfb.log").open("wb") as log:
                # Fixed argv, no shell: the executable comes from PATH and
                # every other argument is a constant, an integer, or this
                # display's own temp directory.
                process = subprocess.Popen(  # nosec B603  # noqa: S603
                    [
                        executable,
                        "-displayfd",
                        str(write_fd),
                        "-screen",
                        "0",
                        f"{int(width)}x{int(height)}x24",
                        "-auth",
                        str(auth_path),
                        "-nolisten",
                        "tcp",
                        # No -terminate: it would let any local user stop
                        # the server with one refused connection attempt.
                    ],
                    pass_fds=(write_fd,),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
        finally:
            os.close(write_fd)
        try:
            number = _read_display_number(read_fd, process)
        except BaseException:
            _terminate(process)
            raise
        finally:
            os.close(read_fd)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return process, f":{number}", directory


def stop(process: subprocess.Popen[bytes], directory: Path) -> None:
    """Terminate the server and delete its cookie directory."""
    _terminate(process)
    shutil.rmtree(directory, ignore_errors=True)


def stop_detached(pid: int, directory: Path) -> None:
    """Stop a display that outlived the process that started it.

    A persistent browser session keeps its display after the opening command
    exits. The PID is signalled only while its command line still names this
    display's cookie directory, so a recycled PID is never killed. Only a
    directory this module created is removed.
    """
    if sys.platform == "win32" or not directory.name.startswith(DIRECTORY_PREFIX):
        return
    if _is_display(pid, directory):
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + STOP_TIMEOUT_S
            while _is_display(pid, directory) and time.monotonic() < deadline:
                time.sleep(0.05)
            if _is_display(pid, directory):
                os.kill(pid, signal.SIGKILL)
    shutil.rmtree(directory, ignore_errors=True)


def _is_display(pid: int, directory: Path) -> bool:
    """True while ``pid`` is the running Xvfb of ``directory``.

    The process that started the display, such as a long-lived MCP server,
    is its parent, so a stopped display stays a zombie until reaped. It is
    reaped here when this process is the parent, and a zombie's empty
    command line counts as stopped either way.
    """
    if sys.platform == "win32":
        return False
    with suppress(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"Xvfb" in command and str(directory).encode() in command


def _read_display_number(fd: int, process: subprocess.Popen[bytes]) -> int:
    """Wait for Xvfb to write its display number to ``fd`` (it does when ready)."""
    if sys.platform == "win32":
        msg = "virtual display needs Linux with Xvfb"
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
    os.set_blocking(fd, False)
    deadline = time.monotonic() + START_TIMEOUT_S
    received = b""
    while time.monotonic() < deadline:
        try:
            chunk = os.read(fd, 32)
        except BlockingIOError:
            chunk = None
        if chunk:
            received += chunk
            if received.endswith(b"\n") and received.strip().isdigit():
                return int(received.strip())
        elif process.poll() is not None:
            msg = f"Xvfb exited during startup (status {process.returncode})"
            raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
        time.sleep(0.02)
    msg = f"Xvfb did not report a display within {START_TIMEOUT_S:.0f}s"
    raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
