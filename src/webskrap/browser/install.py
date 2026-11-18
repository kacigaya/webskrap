"""Download Chrome for Testing, the browser WebSkrap drives by default.

Chrome for Testing is the build Google publishes for automation, for Linux
(x64 and ARM64), macOS and Windows. :func:`install` fetches the current
stable build listed in Google's index and unpacks it below
:func:`browsers_dir`, one folder per version and platform, so an older
install stays usable until the new one is complete.

The index lists no checksums. Integrity rests on HTTPS to Google's storage
host, as with Playwright's own browser downloads. Archive entries that would
land outside the target folder are refused.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import IO, Any
from urllib.parse import urlsplit

from webskrap.errors import ErrorCode, WebSkrapError

#: Google's index of the newest Chrome for Testing build per channel.
INDEX_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)
#: Hosts an index or archive URL may point to.
_ALLOWED_HOSTS = frozenset({"googlechromelabs.github.io", "storage.googleapis.com"})
#: A generic agent with no contact details, as for every diagnostic request.
_USER_AGENT = "Mozilla/5.0 (compatible; webskrap-installer)"
_TIMEOUT_S = 60
_CHUNK = 1 << 20
#: Larger than any Chrome for Testing archive so far (about 200 MB).
MAX_ARCHIVE_BYTES = 600 * 1024 * 1024
#: Written last, so a folder without it is an interrupted install.
_COMPLETE_MARKER = ".webskrap-complete"


def browsers_dir() -> Path:
    """Return where downloaded browsers live.

    ``WEBSKRAP_BROWSERS_PATH`` wins; otherwise the platform's user cache
    folder: ``~/.cache/webskrap/browsers`` (or ``$XDG_CACHE_HOME``) on Linux,
    ``~/Library/Caches/webskrap/browsers`` on macOS and
    ``webskrap/browsers`` under ``%LOCALAPPDATA%`` on Windows.
    """
    configured = os.environ.get("WEBSKRAP_BROWSERS_PATH")
    if configured:
        return Path(configured)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "webskrap" / "browsers"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "webskrap" / "browsers"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "webskrap" / "browsers"


def platform_key() -> str:
    """Return this machine's Chrome for Testing platform name.

    Raises:
        WebSkrapError: ``browser_launch`` on a platform without a build.
    """
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sys.platform.startswith("linux"):
        return "linux-arm64" if arm else "linux64"
    if sys.platform == "darwin":
        return "mac-arm64" if arm else "mac-x64"
    if sys.platform == "win32":
        return "win64" if machine in ("amd64", "x86_64", "arm64") else "win32"
    msg = f"Chrome for Testing has no build for {sys.platform}/{machine}"
    raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)


def executable_in(folder: Path, key: str) -> Path:
    """Return the browser binary inside an unpacked ``chrome-<key>`` folder."""
    root = folder / f"chrome-{key}"
    if key.startswith("mac"):
        app = "Google Chrome for Testing.app"
        return root / app / "Contents" / "MacOS" / "Google Chrome for Testing"
    if key.startswith("win"):
        return root / "chrome.exe"
    return root / "chrome"


def installed_executables(root: Path | None = None) -> list[Path]:
    """Return completely installed browsers for this platform, newest first."""
    root = browsers_dir() if root is None else root
    key = platform_key()
    found = []
    for folder in root.glob(f"chrome-*-{key}") if root.is_dir() else ():
        executable = executable_in(folder, key)
        if (folder / _COMPLETE_MARKER).is_file() and executable.is_file():
            found.append((_version_tuple(folder.name), executable))
    return [executable for _version, executable in sorted(found, reverse=True)]


def _version_tuple(folder_name: str) -> tuple[int, ...]:
    # "chrome-156.0.8078.47-linux64" -> (156, 0, 8078, 47)
    parts = folder_name.split("-")
    try:
        return tuple(int(part) for part in parts[1].split("."))
    except (IndexError, ValueError):
        return ()


def _checked_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS:
        msg = f"refusing to download from {url}: not a Chrome for Testing URL"
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
    return url


def _open(url: str) -> Any:
    request = urllib.request.Request(  # noqa: S310 - https only, checked above
        _checked_url(url), headers={"User-Agent": _USER_AGENT}
    )
    return urllib.request.urlopen(request, timeout=_TIMEOUT_S)  # noqa: S310  # nosec B310


def latest_download(channel: str = "Stable") -> tuple[str, str]:
    """Return ``(version, archive URL)`` of the newest build for this platform.

    Raises:
        WebSkrapError: ``browser_launch`` when the index cannot be read or
            lists no build for this platform.
    """
    key = platform_key()
    try:
        with _open(INDEX_URL) as response:
            index = json.load(response)
        entry = index["channels"][channel]
        downloads = entry["downloads"]["chrome"]
        url = next(item["url"] for item in downloads if item["platform"] == key)
        return str(entry["version"]), _checked_url(url)
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        msg = f"could not read the Chrome for Testing index for {key}: {exc}"
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH) from exc


def _download(url: str, target: IO[bytes]) -> int:
    size = 0
    with _open(url) as response:
        while chunk := response.read(_CHUNK):
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                msg = "the browser archive is larger than expected; refusing it"
                raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
            target.write(chunk)
    return size


def _safe_member(name: str) -> PurePosixPath:
    """Return ``name`` as a relative path, refusing absolute or ``..`` paths."""
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or any(":" in part for part in path.parts)
    ):
        msg = f"refusing archive entry outside the install folder: {name}"
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
    return path


def extract(archive: Path, destination: Path) -> None:
    """Unpack a Chrome for Testing zip with file modes and symlinks restored.

    ``zipfile`` drops both: executables would lose their exec bit, and the
    macOS app bundle relies on symlinks inside its frameworks. A symlink
    whose target leaves ``destination`` is refused.

    Raises:
        WebSkrapError: ``browser_launch`` for an unsafe entry.
    """
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            relative = _safe_member(info.filename)
            target = destination.joinpath(*relative.parts)
            mode = info.external_attr >> 16
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode) and sys.platform != "win32":
                link = bundle.read(info).decode("utf-8")
                resolved = (target.parent / link).resolve()
                if not resolved.is_relative_to(root):
                    msg = f"refusing symlink outside the install folder: {info.filename}"
                    raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
                target.symlink_to(link)
                continue
            with bundle.open(info) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink, _CHUNK)
            if mode & 0o777:
                target.chmod(mode & 0o777)


def install(*, force: bool = False, root: Path | None = None) -> dict[str, Any]:
    """Install the newest stable Chrome for Testing unless it is already here.

    Downloads into a temporary file in ``root``, unpacks into a temporary
    folder, then renames it into place, so an interrupted install never
    replaces a working one. Older versions are kept; remove them by hand.

    Returns:
        ``{"version", "platform", "executable", "installed"}``, where
        ``installed`` is False when that version was already present.

    Raises:
        WebSkrapError: ``browser_launch`` when the download or unpacking fails.
    """
    root = browsers_dir() if root is None else root
    key = platform_key()
    version, url = latest_download()
    folder = root / f"chrome-{version}-{key}"
    executable = executable_in(folder, key)
    done = {"version": version, "platform": key, "executable": str(executable)}
    if not force and (folder / _COMPLETE_MARKER).is_file() and executable.is_file():
        return {**done, "installed": False}
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
    try:
        archive = staging / "chrome.zip"
        try:
            with archive.open("wb") as sink:
                _download(url, sink)
        except OSError as exc:
            msg = f"could not download Chrome for Testing {version}: {exc}"
            raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH) from exc
        unpacked = staging / "unpacked"
        extract(archive, unpacked)
        archive.unlink()
        if not executable_in(unpacked, key).is_file():
            msg = f"the Chrome for Testing {version} archive has no browser for {key}"
            raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
        (unpacked / _COMPLETE_MARKER).write_text(version, encoding="utf-8")
        if folder.exists():
            shutil.rmtree(folder)
        unpacked.rename(folder)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {**done, "installed": True}
