"""Tests for the Chrome for Testing installer, without network access."""

from __future__ import annotations

import io
import json
import stat
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from webskrap import install
from webskrap.errors import ErrorCode, WebSkrapError


@pytest.mark.parametrize(
    ("system", "machine", "key"),
    [
        ("linux", "x86_64", "linux64"),
        ("linux", "aarch64", "linux-arm64"),
        ("darwin", "arm64", "mac-arm64"),
        ("darwin", "x86_64", "mac-x64"),
        ("win32", "AMD64", "win64"),
    ],
)
def test_platform_key_maps_each_supported_platform(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str, key: str
) -> None:
    monkeypatch.setattr(install.sys, "platform", system)
    monkeypatch.setattr(install.platform, "machine", lambda: machine)
    assert install.platform_key() == key


def test_platform_key_refuses_an_unknown_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install.sys, "platform", "freebsd14")
    with pytest.raises(WebSkrapError) as error:
        install.platform_key()
    assert error.value.code is ErrorCode.BROWSER_LAUNCH


def test_executable_paths_follow_the_archive_layout(tmp_path: Path) -> None:
    assert install.executable_in(tmp_path, "linux64") == tmp_path / "chrome-linux64" / "chrome"
    assert install.executable_in(tmp_path, "win64") == tmp_path / "chrome-win64" / "chrome.exe"
    assert install.executable_in(tmp_path, "mac-arm64").name == "Google Chrome for Testing"


def _zip(entries: list[tuple[str, bytes, int]]) -> bytes:
    """Build a zip whose entries carry Unix modes, as Chrome for Testing's do."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            bundle.writestr(info, data)
    return buffer.getvalue()


def _archive(tmp_path: Path, entries: list[tuple[str, bytes, int]]) -> Path:
    path = tmp_path / "chrome.zip"
    path.write_bytes(_zip(entries))
    return path


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and symlinks")
def test_extract_restores_modes_and_symlinks(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path,
        [
            ("chrome-linux64/chrome", b"binary", stat.S_IFREG | 0o755),
            ("chrome-linux64/data.pak", b"data", stat.S_IFREG | 0o644),
            ("chrome-linux64/Current", b"data.pak", stat.S_IFLNK | 0o777),
        ],
    )
    out = tmp_path / "out"
    install.extract(archive, out)

    assert (out / "chrome-linux64" / "chrome").stat().st_mode & 0o777 == 0o755
    link = out / "chrome-linux64" / "Current"
    assert link.is_symlink() and link.read_bytes() == b"data"


@pytest.mark.parametrize(
    "entry",
    [
        ("../escape", b"x", stat.S_IFREG | 0o644),
        ("/etc/escape", b"x", stat.S_IFREG | 0o644),
        ("chrome-linux64/link", b"../../outside", stat.S_IFLNK | 0o777),
    ],
)
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and symlinks")
def test_extract_refuses_entries_outside_the_folder(
    tmp_path: Path, entry: tuple[str, bytes, int]
) -> None:
    archive = _archive(tmp_path, [entry])
    with pytest.raises(WebSkrapError) as error:
        install.extract(archive, tmp_path / "out")
    assert error.value.code is ErrorCode.BROWSER_LAUNCH
    assert not (tmp_path / "escape").exists()


def test_downloads_only_from_chrome_for_testing_hosts() -> None:
    for url in (
        "http://storage.googleapis.com/chrome.zip",
        "https://example.com/chrome.zip",
        "https://storage.googleapis.com.evil.test/chrome.zip",
    ):
        with pytest.raises(WebSkrapError):
            install._checked_url(url)
    assert install._checked_url("https://storage.googleapis.com/x/chrome.zip")


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _fake_network(monkeypatch: pytest.MonkeyPatch, archive: bytes, key: str) -> list[str]:
    opened: list[str] = []
    index = {
        "channels": {
            "Stable": {
                "version": "156.0.8078.47",
                "downloads": {
                    "chrome": [
                        {
                            "platform": key,
                            "url": "https://storage.googleapis.com/cft/chrome.zip",
                        }
                    ]
                },
            }
        }
    }

    def fake_open(url: str) -> Any:
        opened.append(url)
        if url == install.INDEX_URL:
            return _Response(json.dumps(index).encode())
        return _Response(archive)

    monkeypatch.setattr(install, "_open", fake_open)
    return opened


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_install_unpacks_once_and_marks_it_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = install.platform_key()
    binary = install.executable_in(Path(), key).as_posix()
    opened = _fake_network(monkeypatch, _zip([(binary, b"bin", stat.S_IFREG | 0o755)]), key)

    first = install.install(root=tmp_path)
    second = install.install(root=tmp_path)

    assert first["installed"] is True and second["installed"] is False
    assert first["version"] == "156.0.8078.47"
    assert Path(first["executable"]).stat().st_mode & 0o111
    assert install.installed_executables(tmp_path) == [Path(first["executable"])]
    # The second call reads the index but downloads nothing.
    assert opened.count(install.INDEX_URL) == 2 and len(opened) == 3
    # No staging folder is left behind.
    assert [path.name for path in tmp_path.iterdir()] == [f"chrome-156.0.8078.47-{key}"]


def test_install_refuses_an_archive_without_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = install.platform_key()
    _fake_network(monkeypatch, _zip([("README", b"x", stat.S_IFREG | 0o644)]), key)
    with pytest.raises(WebSkrapError) as error:
        install.install(root=tmp_path)
    assert error.value.code is ErrorCode.BROWSER_LAUNCH
    assert list(tmp_path.iterdir()) == []


def test_browsers_dir_honors_the_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSERS_PATH", str(tmp_path))
    assert install.browsers_dir() == tmp_path
