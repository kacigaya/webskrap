"""A private Xvfb display, so a "headless" run can be a real headed browser.

Headless Chrome announces itself: ``HeadlessChrome`` in the user agent, plus
the flags Playwright only adds in headless mode (hidden scrollbars, forced
hover/pointer types). Rewriting the user agent with ``--user-agent`` makes
Chromium drop every high-entropy client hint, which no real browser does.
Running headed on an invisible X server avoids the whole class instead of
patching one token of it.

The server is private to the session: it listens on no TCP port and requires
an MIT-MAGIC-COOKIE held in an owner-only file, so another local user cannot
connect to screenshot the page or inject input.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import struct
import subprocess  # nosec B404  # noqa: S404 - fixed argv for Xvfb, never a shell
import sys
import tempfile
import time
from pathlib import Path

from webskrap.errors import ErrorCode, WebSkrapError

START_TIMEOUT_S = 10.0
STOP_TIMEOUT_S = 5.0
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


class VirtualDisplay:
    """One running Xvfb server and the environment a client needs to use it.

    Create with :meth:`start`; :meth:`stop` terminates the server and removes
    its cookie file. Both are safe to call from async code. A process killed
    without running :meth:`stop` (SIGKILL) leaves the server running, as it
    would a temporary browser profile.
    """

    def __init__(self, process: subprocess.Popen[bytes], display: str, directory: Path) -> None:
        """Adopt a started server; use :meth:`start` rather than calling this."""
        self._process = process
        self.display = display
        self._directory = directory

    @property
    def env(self) -> dict[str, str]:
        """Return ``DISPLAY`` and ``XAUTHORITY`` for a browser on this display."""
        return {"DISPLAY": self.display, "XAUTHORITY": str(self._directory / "Xauthority")}

    @classmethod
    async def start(cls, width: int, height: int) -> VirtualDisplay:
        """Start a private Xvfb with one ``width`` x ``height`` 24-bit screen.

        Raises:
            WebSkrapError: If the host is not Linux, Xvfb is not installed, or
                the server exits or does not become ready in time.
        """
        return await asyncio.to_thread(cls._start, width, height)

    @classmethod
    def _start(cls, width: int, height: int) -> VirtualDisplay:
        if not sys.platform.startswith("linux"):
            msg = "virtual_display needs Linux with Xvfb; use headless or headed mode here"
            raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
        executable = shutil.which("Xvfb")
        if executable is None:
            msg = "virtual_display needs Xvfb. Install it (Debian/Ubuntu: apt install xvfb)"
            raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)

        directory = Path(tempfile.mkdtemp(prefix="webskrap-display-"))
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
        return cls(process, f":{number}", directory)

    async def stop(self) -> None:
        """Terminate the server and delete its cookie file. Idempotent."""
        await asyncio.to_thread(self._stop)

    def _stop(self) -> None:
        _terminate(self._process)
        shutil.rmtree(self._directory, ignore_errors=True)


def _read_display_number(fd: int, process: subprocess.Popen[bytes]) -> int:
    """Wait for Xvfb to write its display number to ``fd`` (it does when ready)."""
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
