"""Locate installed Chromium browser executables."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from webskrap.browser.install import browsers_dir, installed_executables
from webskrap.errors import ErrorCode, WebSkrapError


def _channel_paths(channel: str) -> list[Path]:
    """Return where ``chrome`` or ``msedge`` installs on this platform."""
    if sys.platform == "win32":
        bases = [
            os.environ.get(name) for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        ]
        folder = ("Google", "Chrome") if channel == "chrome" else ("Microsoft", "Edge")
        binary = "chrome.exe" if channel == "chrome" else "msedge.exe"
        return [Path(base, *folder, "Application", binary) for base in bases if base]
    if sys.platform == "darwin":
        app = "Google Chrome" if channel == "chrome" else "Microsoft Edge"
        return [Path(f"/Applications/{app}.app/Contents/MacOS/{app}")]
    if channel == "chrome":
        return [Path("/opt/google/chrome/chrome")]
    return [Path("/opt/microsoft/msedge/msedge")]


#: Distribution Chromium builds, used when nothing was downloaded.
_SYSTEM_CHROMIUM = (
    Path("/usr/bin/chromium"),
    Path("/usr/bin/chromium-browser"),
    Path("/snap/bin/chromium"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
)


def _playwright_browsers_dirs() -> list[Path]:
    """Return where WebSkrap 2.x had Playwright put browsers, per Playwright's rules.

    ``PLAYWRIGHT_BROWSERS_PATH=0`` means each driver package keeps its own
    ``.local-browsers`` directory. ``find_spec`` locates the packages without
    importing them, so no driver code runs.
    """
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured == "0":
        roots = []
        for package in ("playwright", "patchright"):
            spec = importlib.util.find_spec(package)
            if spec is not None and spec.origin is not None:
                roots.append(Path(spec.origin).parent / "driver" / "package" / ".local-browsers")
        return roots
    if configured:
        return [Path(configured)]
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Caches" / "ms-playwright"]
    return [Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ms-playwright"]


def _revision(path: Path) -> int:
    suffix = path.name.rpartition("-")[2]
    return int(suffix) if suffix.isdigit() else -1


def find_executable(channel: str | None) -> Path:
    """Return the browser binary for ``channel``.

    ``chrome`` and ``msedge`` are looked up at their standard install paths.
    ``None`` and ``chromium`` use, in order: the newest Chrome for Testing
    that ``webskrap install`` downloaded, a Chromium that Playwright or
    Patchright downloaded for WebSkrap 2.x, then a distribution Chromium.

    Raises:
        WebSkrapError: ``browser_launch`` when no binary exists; the message
            matches the launch-failure markers channel fallback retries on.
    """
    if channel in ("chrome", "msedge"):
        candidates = _channel_paths(channel)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        where = candidates[0] if candidates else channel
        msg = f"Chromium distribution '{channel}' is not found at {where}"
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
    if channel not in (None, "chromium"):
        msg = f"channel '{channel}' must be one of: chrome, msedge, chromium"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    # Imported here: the installer is only needed for the downloaded browser.
    downloaded = installed_executables()
    if downloaded:
        return downloaded[0]
    roots = _playwright_browsers_dirs()
    patterns = (
        "chrome-linux*/chrome",
        "chrome-win/chrome.exe",
        "chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        "chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    )
    revision_dirs = [path for root in roots for path in root.glob("chromium-*")]
    for revision_dir in sorted(revision_dirs, key=_revision, reverse=True):
        for pattern in patterns:
            for candidate in revision_dir.glob(pattern):
                if candidate.is_file():
                    return candidate
    for candidate in _SYSTEM_CHROMIUM:
        if candidate.is_file():
            return candidate
    msg = (
        f"Chromium is not installed. Executable doesn't exist under {browsers_dir()}. "
        "Run: webskrap install"
    )
    raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
