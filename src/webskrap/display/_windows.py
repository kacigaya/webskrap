"""Lease preconfigured Virtual Display Driver monitors without driver control."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from webskrap.errors import ErrorCode, WebSkrapError

if sys.platform == "win32":
    import msvcrt


class _Device(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


class _Mode(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("x", wintypes.LONG),
        ("y", wintypes.LONG),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", wintypes.SHORT),
        ("dmDuplex", wintypes.SHORT),
        ("dmYResolution", wintypes.SHORT),
        ("dmTTOption", wintypes.SHORT),
        ("dmCollate", wintypes.SHORT),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class _Monitor:
    name: str
    attached: bool
    identity: str = ""


def _error(message: str) -> WebSkrapError:
    return WebSkrapError(message, code=ErrorCode.BROWSER_LAUNCH)


def _is_vdd(device_id: str, description: str) -> bool:
    hardware = device_id.casefold().replace("/", "\\")
    return (
        hardware == "root\\mttvdd"
        or hardware.startswith("root\\mttvdd\\")
        or (description.casefold() == "virtual display driver")
    )


def _devices(parent: str | None = None) -> Iterator[_Device]:
    if sys.platform != "win32":
        return
    function = ctypes.WinDLL("user32", use_last_error=True).EnumDisplayDevicesW
    function.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_Device), wintypes.DWORD]
    function.restype = wintypes.BOOL
    index = 0
    while True:
        device = _Device()
        device.cb = ctypes.sizeof(device)
        if not function(parent, index, ctypes.byref(device), 0):
            break
        yield device
        index += 1


def _monitors() -> list[_Monitor]:
    result: list[_Monitor] = []
    for adapter in _devices():
        children = list(_devices(adapter.DeviceName))
        if any(_is_vdd(d.DeviceID, d.DeviceString) for d in [adapter, *children]):
            result.append(
                _Monitor(adapter.DeviceName, bool(adapter.StateFlags & 1), adapter.DeviceKey)
            )
    return result


def _mode(name: str, index: int = -1) -> _Mode | None:
    if sys.platform != "win32":
        return None
    function = ctypes.WinDLL("user32", use_last_error=True).EnumDisplaySettingsW
    function.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_Mode)]
    function.restype = wintypes.BOOL
    mode = _Mode()
    mode.dmSize = ctypes.sizeof(mode)
    return mode if function(name, index, ctypes.byref(mode)) else None


def _modes(name: str) -> list[_Mode]:
    result: list[_Mode] = []
    index = 0
    while (mode := _mode(name, index)) is not None:
        result.append(mode)
        index += 1
    return result


def _change(name: str, mode: _Mode) -> None:
    if sys.platform != "win32":
        raise _error("Windows virtual displays require Windows")
    function = ctypes.WinDLL("user32", use_last_error=True).ChangeDisplaySettingsExW
    function.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(_Mode),
        wintypes.HWND,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    function.restype = wintypes.LONG
    status = function(name, ctypes.byref(mode), None, 0, None)
    if status != 0:
        raise _error(f"ChangeDisplaySettingsExW failed for {name}: DISP_CHANGE code {status}")


def _right_edge() -> int:
    return max(
        (
            mode.x + mode.dmPelsWidth
            for device in _devices()
            if device.StateFlags & 1 and (mode := _mode(device.DeviceName)) is not None
        ),
        default=0,
    )


def _state_path(name: str, suffix: str) -> Path:
    directory = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    directory = directory / "webskrap" / "display-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = re.sub(r"[^a-z0-9_-]", "_", name.casefold()) + suffix
    return directory / filename


def _recover(monitor: _Monitor) -> None:
    path = _state_path(monitor.name, ".json")
    if not path.exists():
        return
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("identity") != monitor.identity:
            raise ValueError("monitor identity changed")
        raw = bytes.fromhex(state["mode"])
        if len(raw) != ctypes.sizeof(_Mode):
            raise ValueError("display mode size changed")
        previous = _Mode.from_buffer_copy(raw)
        if previous.dmSize != ctypes.sizeof(_Mode) or previous.dmDriverExtra:
            raise ValueError("invalid saved mode")
    except (ValueError, KeyError, TypeError) as exc:
        raise _error(f"Cannot recover VDD settings from {path}: {exc}") from exc
    _change(monitor.name, previous)
    path.unlink()


def _save_restore(monitor: _Monitor, mode: _Mode) -> Path:
    path = _state_path(monitor.name, ".json")
    temporary = path.with_suffix(".tmp")
    state = {"identity": monitor.identity, "mode": bytes(mode).hex()}
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(state, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    return path


@contextmanager
def _claim(name: str, *, wait: bool = False) -> Iterator[bool]:
    if sys.platform != "win32":
        raise _error("Windows virtual displays require Windows")
    with _state_path(name, ".lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            if wait:
                raise _error("Timed out waiting for another VDD desktop layout change") from exc
            yield False
        else:
            try:
                yield True
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def available() -> bool:
    """Return whether Windows exposes a configured VDD monitor. Never raise."""
    if sys.platform != "win32":
        return False
    try:
        return bool(_monitors())
    except Exception:
        return False


@contextmanager
def hold(width: int, height: int) -> Iterator[tuple[int, int]]:
    """Claim a VDD monitor, set its size, and restore it before releasing it."""
    if width <= 0 or height <= 0:
        raise _error("Virtual display width and height must be positive")
    monitors = _monitors()
    if not monitors:
        raise _error(
            "No VDD monitor found. Install the Virtual Display Driver and configure its "
            "monitors; see the virtual_display setup docs."
        )
    for monitor in monitors:
        with _claim(monitor.name) as claimed:
            if not claimed:
                continue
            with _claim("_desktop_layout", wait=True):
                refreshed = next((m for m in _monitors() if m.name == monitor.name), None)
                if refreshed is None:
                    raise _error(f"VDD monitor {monitor.name} disappeared; check the driver")
                monitor = refreshed
                _recover(monitor)
                monitor = next(m for m in _monitors() if m.name == monitor.name)
                previous = _mode(monitor.name)
                if previous is None:
                    previous = _mode(monitor.name, -2)
                if previous is None:
                    raise _error(f"Cannot read the previous display mode for {monitor.name}")
                modes = _modes(monitor.name)
                selected = next(
                    (m for m in modes if (m.dmPelsWidth, m.dmPelsHeight) == (width, height)), None
                )
                if selected is None:
                    offered = ", ".join(
                        sorted({f"{m.dmPelsWidth}x{m.dmPelsHeight}" for m in modes})
                    )
                    raise _error(
                        f"VDD resolution {width}x{height} is not offered. Available modes: "
                        f"{offered or 'none'}. Add it to <resolutions> in vdd_settings.xml."
                    )
                selected.x = previous.x if monitor.attached else _right_edge()
                selected.y = previous.y if monitor.attached else 0
                selected.dmFields |= 0x20
                restore = _Mode.from_buffer_copy(previous)
                if not monitor.attached:
                    restore.dmFields = 0x20 | 0x80000 | 0x100000
                    restore.dmPelsWidth = restore.dmPelsHeight = 0
                journal = _save_restore(monitor, restore)
                try:
                    _change(monitor.name, selected)
                    current = _mode(monitor.name)
                    if current is None or (current.dmPelsWidth, current.dmPelsHeight) != (
                        width,
                        height,
                    ):
                        raise _error(f"VDD did not apply the requested {width}x{height} mode")
                except BaseException:
                    _change(monitor.name, restore)
                    journal.unlink()
                    raise
            try:
                yield current.x, current.y
            finally:
                with _claim("_desktop_layout", wait=True):
                    _change(monitor.name, restore)
                    journal.unlink()
            return
    raise _error(
        f"All {len(monitors)} VDD monitors are in use. Raise <count> in vdd_settings.xml "
        "before starting more sessions."
    )
