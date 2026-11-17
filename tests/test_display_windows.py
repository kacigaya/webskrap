"""Windows display leases with a fake Win32 boundary and optional real driver."""

import ctypes
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from webskrap.display import _windows
from webskrap.errors import ErrorCode, WebSkrapError


def mode(width=1920, height=1080, x=0, y=0):
    result = _windows._Mode()
    result.dmSize = ctypes.sizeof(result)
    result.dmPelsWidth, result.dmPelsHeight = width, height
    result.x, result.y = x, y
    return result


@pytest.mark.parametrize(
    ("hardware", "description", "expected"),
    [
        (r"ROOT\MTTVDD\0000", "", True),
        (r"Root\MttVDD", "", True),
        ("", "Virtual Display Driver", True),
        (r"Root\MttVDDOther", "Generic Monitor", False),
        ("", "Other Virtual Display Driver", False),
    ],
)
def test_matcher(hardware, description, expected):
    assert _windows._is_vdd(hardware, description) is expected


@pytest.fixture
def win32(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    current = {"one": mode(x=1920), "two": mode(x=3840)}
    changes = []
    claims = []
    busy = set()

    @contextmanager
    def claim(name, *, wait=False):
        if wait:
            yield True
            return
        claims.append((name, "enter"))
        try:
            yield name not in busy
        finally:
            claims.append((name, "exit"))

    def change(name, value):
        changes.append((name, value.dmPelsWidth, value.dmPelsHeight, value.x, value.y))
        current[name] = _windows._Mode.from_buffer_copy(value)

    monkeypatch.setattr(
        _windows,
        "_monitors",
        lambda: [_windows._Monitor("one", True), _windows._Monitor("two", True)],
    )
    monkeypatch.setattr(_windows, "_claim", claim)
    monkeypatch.setattr(_windows, "_mode", lambda name, index=-1: current[name])
    monkeypatch.setattr(_windows, "_modes", lambda name: [mode(1280, 720), mode()])
    monkeypatch.setattr(_windows, "_change", change)
    return current, changes, claims, busy


@pytest.mark.parametrize("busy_first", [False, True])
def test_claim_first_free(win32, busy_first):
    current, changes, claims, busy = win32
    if busy_first:
        busy.add("one")
    name = "two" if busy_first else "one"
    with _windows.hold(1280, 720) as position:
        assert position == (3840 if busy_first else 1920, 0)
        assert current[name].dmPelsWidth == 1280
    assert current[name].dmPelsWidth == 1920
    assert changes[-1][0] == name
    assert claims[-1] == (name, "exit")


def test_restore_on_exception(win32):
    current, changes, claims, _ = win32
    with pytest.raises(ValueError, match="browser failed"), _windows.hold(1280, 720):
        raise ValueError("browser failed")
    assert current["one"].dmPelsWidth == 1920
    assert changes[-1][1:3] == (1920, 1080)
    assert claims[-1] == ("one", "exit")


def test_mode_not_offered(win32):
    _, changes, claims, _ = win32
    with (
        pytest.raises(WebSkrapError, match=r"1280x720.*1920x1080.*<resolutions>") as caught,
        _windows.hold(800, 600),
    ):
        pytest.fail("unavailable mode was accepted")
    assert caught.value.code == ErrorCode.BROWSER_LAUNCH
    assert changes == []
    assert claims[-1] == ("one", "exit")


def test_all_busy(win32):
    win32[3].update({"one", "two"})
    with pytest.raises(WebSkrapError, match=r"All 2.*<count>"), _windows.hold(1280, 720):
        pytest.fail("busy monitor was claimed")


def test_no_monitor(monkeypatch):
    monkeypatch.setattr(_windows, "_monitors", lambda: [])
    with (
        pytest.raises(WebSkrapError, match="Install the Virtual Display Driver"),
        _windows.hold(1280, 720),
    ):
        pytest.fail("missing driver was accepted")


def test_attach_and_detach(win32, monkeypatch):
    current, changes, _, _ = win32
    monkeypatch.setattr(_windows, "_monitors", lambda: [_windows._Monitor("one", False)])
    monkeypatch.setattr(_windows, "_right_edge", lambda: 5760)
    with _windows.hold(1280, 720) as position:
        assert position == (5760, 0)
    assert changes[-1][1:3] == (0, 0)
    assert current["one"].dmFields == 0x180020


def test_readback_failure_restores(win32, monkeypatch):
    _, changes, _, _ = win32
    monkeypatch.setattr(_windows, "_change", lambda name, value: changes.append(value.dmPelsWidth))
    with pytest.raises(WebSkrapError, match="did not apply"), _windows.hold(1280, 720):
        pytest.fail("incorrect mode was accepted")
    assert changes == [1280, 1920]


def test_available_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert not _windows.available()


def test_available_never_raises(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def fail():
        raise OSError("driver unavailable")

    monkeypatch.setattr(_windows, "_monitors", fail)
    assert not _windows.available()


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows byte-range locks")
def test_real_claim_skips_locked_monitor(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with _windows._claim(r"\\.\DISPLAY1") as first:
        assert first
        with _windows._claim(r"\\.\DISPLAY1") as second:
            assert not second
    with _windows._claim(r"\\.\DISPLAY1") as released:
        assert released
    assert len(list(tmp_path.rglob("*.lock"))) == 1


@pytest.mark.skipif(
    sys.platform != "win32" or not _windows.available(), reason="requires Windows with VDD"
)
def test_real_display_restores():
    monitor = _windows._monitors()[0]
    previous = _windows._mode(monitor.name)
    assert previous is not None
    with _windows.hold(1280, 720) as position:
        current = _windows._mode(monitor.name)
        assert current is not None
        assert (current.dmPelsWidth, current.dmPelsHeight) == (1280, 720)
        assert position == (current.x, current.y)
    restored = _windows._mode(monitor.name)
    if monitor.attached:
        assert restored is not None
        assert (restored.dmPelsWidth, restored.dmPelsHeight, restored.x, restored.y) == (
            previous.dmPelsWidth,
            previous.dmPelsHeight,
            previous.x,
            previous.y,
        )
    else:
        assert not next(m for m in _windows._monitors() if m.name == monitor.name).attached


def test_enumerates_adapter_and_monitor_identity(monkeypatch):
    adapter = _windows._Device()
    adapter.DeviceName = r"\\.\DISPLAY1"
    adapter.DeviceString = "Virtual Display Driver"
    adapter.StateFlags = 1
    other = _windows._Device()
    other.DeviceName = r"\\.\DISPLAY2"
    monitor = _windows._Device()
    monitor.DeviceID = r"ROOT\MTTVDD\0000"
    unrelated = _windows._Device()
    unrelated.DeviceName = r"\\.\DISPLAY3"
    devices = {
        None: [adapter, other, unrelated],
        adapter.DeviceName: [],
        other.DeviceName: [monitor],
        unrelated.DeviceName: [],
    }
    monkeypatch.setattr(_windows, "_devices", lambda parent=None: iter(devices[parent]))
    assert _windows._monitors() == [
        _windows._Monitor(adapter.DeviceName, True),
        _windows._Monitor(other.DeviceName, False),
    ]


def test_failed_mode_change_restores(win32, monkeypatch):
    _, changes, claims, _ = win32

    def fail_requested(name, value):
        changes.append(value.dmPelsWidth)
        if value.dmPelsWidth == 1280:
            raise WebSkrapError("DISP_CHANGE code -2", code=ErrorCode.BROWSER_LAUNCH)

    monkeypatch.setattr(_windows, "_change", fail_requested)
    with pytest.raises(WebSkrapError, match="DISP_CHANGE code -2"), _windows.hold(1280, 720):
        pytest.fail("failed mode change was accepted")
    assert changes == [1280, 1920]
    assert claims[-1] == ("one", "exit")


def test_detached_monitor_uses_registry_mode(win32, monkeypatch):
    current, changes, _, _ = win32
    saved = current["one"]
    monkeypatch.setattr(_windows, "_monitors", lambda: [_windows._Monitor("one", False)])
    monkeypatch.setattr(_windows, "_right_edge", lambda: 1920)
    monkeypatch.setattr(
        _windows,
        "_mode",
        lambda name, index=-1: saved if index == -2 else (current[name] if changes else None),
    )
    with _windows.hold(1280, 720) as position:
        assert position == (1920, 0)
    assert changes[-1][1:3] == (0, 0)


def test_recovers_interrupted_lease_before_reuse(win32):
    current, changes, _, _ = win32
    monitor = _windows._Monitor("one", True)
    journal = _windows._save_restore(monitor, mode())
    current["one"] = mode(1280, 720)
    with _windows.hold(1280, 720):
        assert changes[0][1:3] == (1920, 1080)
    assert not journal.exists()
    assert current["one"].dmPelsWidth == 1920


def test_recovery_rejects_changed_monitor_identity(win32):
    _, changes, _, _ = win32
    _windows._save_restore(_windows._Monitor("one", True, "previous-device"), mode())
    with pytest.raises(WebSkrapError, match="monitor identity changed"), _windows.hold(1280, 720):
        pytest.fail("unrelated monitor settings were changed")
    assert changes == []
