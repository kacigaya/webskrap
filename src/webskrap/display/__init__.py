"""A private virtual display, so a "headless" run can be a real headed browser.

Headless Chrome announces itself: ``HeadlessChrome`` in the user agent, plus
the flags headless automation adds (hidden scrollbars, forced
hover/pointer types). Rewriting the user agent with ``--user-agent`` makes
Chromium drop every high-entropy client hint, which no real browser does.
Running headed on an invisible screen avoids the whole class instead of
patching one token of it. No backend falls back to headless mode or to
``--screen-info``.

Each platform has its own backend behind :class:`VirtualDisplay`:

- Linux: a private Xvfb server (:mod:`webskrap.display._xvfb`). The browser
  reaches it through ``DISPLAY`` and ``XAUTHORITY``.
- macOS: a CoreGraphics ``CGVirtualDisplay`` (:mod:`webskrap.display._macos`).
- Windows: one monitor of the open-source Virtual Display Driver, an IddCx
  driver an administrator installs (:mod:`webskrap.display._windows`).

On macOS and Windows a helper process (:mod:`webskrap.display._helper`) holds
the display, so it outlives the command that opened a persistent session,
and the browser window is placed on it with ``--window-position``.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess  # nosec B404  # noqa: S404 - only types and a terminate call
import sys
from pathlib import Path

from webskrap.display import _helper, _xvfb
from webskrap.display._xvfb import (
    DIRECTORY_PREFIX,
    START_TIMEOUT_S,
    STOP_TIMEOUT_S,
    write_cookie_file,
)
from webskrap.errors import ErrorCode, WebSkrapError

__all__ = [
    "DIRECTORY_PREFIX",
    "START_TIMEOUT_S",
    "STOP_TIMEOUT_S",
    "VirtualDisplay",
    "available",
    "place_window",
    "stop_detached",
    "write_cookie_file",
]


class VirtualDisplay:
    """One running virtual display and what a browser needs to use it.

    Create with :meth:`start`; :meth:`stop` releases the display and removes
    its owner-only directory. Both are safe to call from async code. A
    process killed without running :meth:`stop` (SIGKILL) leaves a Linux
    Xvfb running, as it would a temporary browser profile. On macOS and
    Windows the helper notices that its owner is gone and releases the
    display, unless it was started ``detached``.
    """

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        display: str,
        directory: Path,
        origin: tuple[int, int] = (0, 0),
    ) -> None:
        """Adopt a started display; use :meth:`start` rather than calling this."""
        self._process = process
        self.display = display
        self._directory = directory
        self.origin = origin

    @property
    def pid(self) -> int:
        """Return the process that holds the display (Xvfb or the helper)."""
        return self._process.pid

    @property
    def directory(self) -> Path:
        """Return the owner-only directory of this display."""
        return self._directory

    @property
    def env(self) -> dict[str, str]:
        """Return the variables a browser needs: ``DISPLAY`` and ``XAUTHORITY`` on Linux."""
        if self.display.startswith(":"):
            return {"DISPLAY": self.display, "XAUTHORITY": str(self._directory / "Xauthority")}
        return {}

    @classmethod
    async def start(cls, width: int, height: int, *, detached: bool = False) -> VirtualDisplay:
        """Start a private ``width`` x ``height`` display on this host.

        Args:
            width: Screen width in pixels.
            height: Screen height in pixels.
            detached: Keep the display after this process exits, for a
                persistent browser session. Linux Xvfb always outlives it.

        Raises:
            WebSkrapError: ``browser_launch`` if this host cannot provide a
                display, or it does not become ready in time.
        """
        startup = asyncio.create_task(asyncio.to_thread(cls._start, width, height, detached))
        try:
            return await asyncio.shield(startup)
        except asyncio.CancelledError:
            try:
                virtual_display = await startup
            except Exception:
                virtual_display = None
            else:
                await asyncio.shield(virtual_display.stop())
            raise

    @classmethod
    def _start(cls, width: int, height: int, detached: bool = False) -> VirtualDisplay:
        if sys.platform.startswith("linux"):
            if shutil.which("Xvfb") is None:
                msg = "virtual_display needs Xvfb. Install it (Debian/Ubuntu: apt install xvfb)"
                raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
            process, display, directory = _xvfb.start(width, height)
            return cls(process, display, directory)
        if sys.platform in ("darwin", "win32"):
            process, directory, origin = _helper.start(width, height, detached=detached)
            return cls(process, f"{sys.platform}:{origin[0]},{origin[1]}", directory, origin)
        msg = "virtual_display needs Linux, macOS or Windows; use headless or headed mode here"
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)

    def window_args(self, args: list[str], width: int, height: int) -> list[str]:
        """Return ``args`` with the browser window placed on this display."""
        if self.display.startswith(":"):
            return args
        return place_window(args, self.origin, width, height)

    async def stop(self) -> None:
        """Release the display and delete its directory. Idempotent."""
        await asyncio.to_thread(self._stop)

    def _stop(self) -> None:
        if self.display.startswith(":"):
            _xvfb.stop(self._process, self._directory)
        else:
            _helper.stop(self._process, self._directory)


def place_window(args: list[str], origin: tuple[int, int], width: int, height: int) -> list[str]:
    """Move the window flags in ``args`` onto a screen whose top-left is ``origin``.

    A ``--window-position`` counts from the virtual screen's corner, as it
    does on an Xvfb screen. Without one the window fills the screen.

    Raises:
        WebSkrapError: ``usage`` if a ``--window-position`` is not two integers.
    """
    left, top = origin
    placed: list[str] = []
    positioned = sized = False
    for arg in args:
        if arg.startswith("--window-position="):
            try:
                x, y = (int(value) for value in arg.split("=", 1)[1].split(","))
            except ValueError:
                msg = f"{arg} needs two integers, as in --window-position=0,0"
                raise WebSkrapError(msg, code=ErrorCode.USAGE) from None
            arg = f"--window-position={left + x},{top + y}"
            positioned = True
        sized = sized or arg.startswith("--window-size=")
        placed.append(arg)
    if not positioned:
        placed.append(f"--window-position={left},{top}")
    if not sized:
        placed.append(f"--window-size={width},{height}")
    return placed


def available() -> bool:
    """True when this host can start a virtual display. Never raises."""
    if sys.platform.startswith("linux"):
        return shutil.which("Xvfb") is not None
    if sys.platform == "darwin":
        from webskrap.display import _macos

        return _macos.available()
    if sys.platform == "win32":
        from webskrap.display import _windows

        return _windows.available()
    return False


def stop_detached(pid: int, directory: Path) -> None:
    """Stop a display that outlived the process that started it.

    A persistent browser session keeps its display after the opening command
    exits. Only a directory this package created is touched, and a recycled
    PID is never killed.
    """
    if not directory.name.startswith(DIRECTORY_PREFIX):
        return
    if sys.platform.startswith("linux") and not _helper.is_helper_directory(directory):
        _xvfb.stop_detached(pid, directory)
    else:
        _helper.stop_detached(pid, directory)
