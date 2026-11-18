from __future__ import annotations

import asyncio
import os
import shutil
import stat
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from webskrap import display
from webskrap.client import WebSkrapError
from webskrap.errors import ErrorCode

needs_xvfb = pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("Xvfb") is None,
    reason="needs Linux with Xvfb",
)


def _read_entry(path: Path) -> tuple[int, bytes, bytes, bytes, bytes]:
    data = path.read_bytes()
    (family,) = struct.unpack(">H", data[:2])
    fields, offset = [], 2
    for _ in range(4):
        (length,) = struct.unpack(">H", data[offset : offset + 2])
        fields.append(data[offset + 2 : offset + 2 + length])
        offset += 2 + length
    assert offset == len(data)
    return family, fields[0], fields[1], fields[2], fields[3]


def test_cookie_file_matches_any_display_and_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "Xauthority"

    display.write_cookie_file(path, b"\x01" * 16)

    family, address, number, name, cookie = _read_entry(path)
    # FamilyWild and an empty display number match whatever display Xvfb picks.
    assert (family, address, number) == (0xFFFF, b"", b"")
    assert name == b"MIT-MAGIC-COOKIE-1"
    assert cookie == b"\x01" * 16
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_cookie_file_refuses_to_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "Xauthority"
    path.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        display.write_cookie_file(path, b"\x01" * 16)


async def test_missing_xvfb_is_a_launch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(display.sys, "platform", "linux")
    monkeypatch.setattr(display.shutil, "which", lambda _name: None)

    with pytest.raises(WebSkrapError, match="apt install xvfb") as excinfo:
        await display.VirtualDisplay.start(800, 600)

    assert excinfo.value.code is ErrorCode.BROWSER_LAUNCH


async def test_non_linux_is_a_launch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(display.sys, "platform", "darwin")

    with pytest.raises(WebSkrapError, match="needs Linux"):
        await display.VirtualDisplay.start(800, 600)


async def test_cancelled_start_stops_the_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    stopped: list[bool] = []

    def start_blocking(_width: int, _height: int) -> display.VirtualDisplay:
        started.set()
        release.wait()

        class _Display:
            async def stop(self) -> None:
                stopped.append(True)

        return _Display()  # type: ignore[return-value]

    monkeypatch.setattr(display.VirtualDisplay, "_start", staticmethod(start_blocking))
    task = asyncio.create_task(display.VirtualDisplay.start(800, 600))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert stopped == [True]


def _xdpyinfo(executable: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # Fixed argv resolved from PATH by the caller; nothing comes from input.
    return subprocess.run(  # noqa: S603
        [executable], env={**os.environ, **env}, capture_output=True, text=True, check=False
    )


@needs_xvfb
async def test_display_requires_the_cookie_and_cleans_up() -> None:
    virtual = await display.VirtualDisplay.start(1366, 768)
    auth = Path(virtual.env["XAUTHORITY"])
    try:
        assert virtual.display.startswith(":")
        assert auth.is_file()
        if xdpyinfo := shutil.which("xdpyinfo"):
            # Refused by the live server, not merely unreachable: the cookie
            # holder connects afterwards, proving the server was up.
            no_cookie = {"DISPLAY": virtual.display, "XAUTHORITY": "/nonexistent"}
            refused = _xdpyinfo(xdpyinfo, no_cookie)
            assert refused.returncode != 0
            assert "authorization required" in refused.stderr.lower()
            assert "1366x768 pixels" in _xdpyinfo(xdpyinfo, virtual.env).stdout
    finally:
        await virtual.stop()

    assert not auth.parent.exists()
    await virtual.stop()  # idempotent
