"""CoreGraphics display lifecycle with a fake native runtime."""

from unittest.mock import Mock

import pytest

from webskrap.display import _macos
from webskrap.errors import WebSkrapError

_CREATE = _macos._create


@pytest.fixture
def runtime(monkeypatch):
    native = Mock()
    native.gui_session.return_value = True
    native.send.return_value = 42
    native.cg.CGDisplayPixelsWide.return_value = 1280
    native.cg.CGDisplayPixelsHigh.return_value = 720
    native.cg.CGDisplayBounds.return_value = _macos.CGRect(
        _macos.CGPoint(1920, 0), _macos.CGSize(1280, 720)
    )
    monkeypatch.setattr(_macos, "_Runtime", lambda: native)
    monkeypatch.setattr(_macos, "_create", lambda *_: 17)
    return native


def test_hold_releases_display_and_pool(runtime):
    with _macos.hold(1280, 720) as origin:
        assert origin == (1920, 0)
    runtime.send.assert_any_call(17, "release")
    runtime.objc.objc_autoreleasePoolPop.assert_called_once()


def test_hold_releases_after_browser_error(runtime):
    with pytest.raises(ValueError, match="browser failed"), _macos.hold(1280, 720):
        raise ValueError("browser failed")
    runtime.send.assert_any_call(17, "release")
    runtime.objc.objc_autoreleasePoolPop.assert_called_once()


def test_no_gui_fails_before_creation(runtime):
    runtime.gui_session.return_value = False
    with pytest.raises(WebSkrapError, match="no logged-in GUI session"), _macos.hold(1280, 720):
        pytest.fail("display created outside a GUI session")
    runtime.send.assert_not_called()


def test_missing_id_releases(runtime):
    runtime.send.return_value = 0
    with pytest.raises(WebSkrapError, match="without an ID"), _macos.hold(1280, 720):
        pytest.fail("invalid display accepted")
    runtime.send.assert_any_call(17, "release")


def test_missing_framework(monkeypatch):
    def fail():
        raise OSError("missing framework")

    monkeypatch.setattr(_macos, "_Runtime", fail)
    assert not _macos.available()
    with (
        pytest.raises(WebSkrapError, match="CoreGraphics could not be loaded"),
        _macos.hold(1280, 720),
    ):
        pytest.fail("missing framework accepted")


def test_invalid_dimensions():
    with pytest.raises(WebSkrapError, match="must be positive"), _macos.hold(0, 720):
        pytest.fail("invalid resolution accepted")


def test_waits_for_matching_pixels(runtime, monkeypatch):
    ticks = iter([0, 0, 1, 2])
    monkeypatch.setattr(_macos.time, "monotonic", lambda: next(ticks))
    runtime.online.side_effect = [False, True]
    bounds = _macos._wait_until_online(runtime, 42, 1280, 720)
    assert bounds.origin.x == 1920
    runtime.pump.assert_called_once_with(0.05)


def test_online_timeout(runtime, monkeypatch):
    ticks = iter([0, 0, 11])
    monkeypatch.setattr(_macos.time, "monotonic", lambda: next(ticks))
    runtime.online.return_value = False
    with pytest.raises(WebSkrapError, match="did not come online"):
        _macos._wait_until_online(runtime, 42, 1280, 720)


def test_descriptor_failure_does_not_leak(runtime):
    runtime.new.return_value = 9
    runtime.responds.return_value = True
    runtime.send.side_effect = lambda receiver, selector, *args, **kwargs: (
        0 if selector == "initWithDescriptor:" else 10
    )
    with pytest.raises(WebSkrapError, match="refused to create"):
        _CREATE(runtime, 1280, 720)
    runtime.send.assert_any_call(9, "release")


def test_apply_settings_failure_releases_mode_and_settings(runtime):
    runtime.new.return_value = 9
    runtime.send.side_effect = lambda receiver, selector, *args, **kwargs: (
        False if selector == "applySettings:" else 10
    )
    with pytest.raises(WebSkrapError, match="refused the 1280x720 mode"):
        _macos._apply_mode(runtime, 17, 1280, 720)
    runtime.send.assert_any_call(10, "release")
    runtime.send.assert_any_call(9, "release")
