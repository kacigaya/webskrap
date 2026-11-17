"""CoreGraphics display lifecycle with a fake native runtime."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from webskrap.display import _macos
from webskrap.errors import WebSkrapError

_CREATE = _macos._create
_ARRANGE = _macos._arrange


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
    monkeypatch.setattr(_macos, "_arrange", lambda *_: native.cg.CGDisplayBounds())
    monkeypatch.setattr(_macos, "_layout_lock", nullcontext)
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
    monkeypatch.setattr(_macos, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    runtime.online.side_effect = [False, True]
    bounds = _macos._wait_until_online(runtime, 42, 1280, 720)
    assert bounds.origin.x == 1920
    runtime.pump.assert_called_once_with(0.05)


def test_online_timeout(runtime, monkeypatch):
    ticks = iter([0, 0, 11])
    monkeypatch.setattr(_macos, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
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


def test_arrangement_uses_retina_logical_bounds_and_does_not_move_other_displays(runtime):
    runtime.cg.CGBeginDisplayConfiguration.return_value = 0
    runtime.cg.CGConfigureDisplayMirrorOfDisplay.return_value = 0
    runtime.cg.CGConfigureDisplayOrigin.return_value = 0
    runtime.cg.CGCompleteDisplayConfiguration.return_value = 0
    runtime.displays.return_value = [1, 42]
    physical = _macos.CGRect(_macos.CGPoint(0, 0), _macos.CGSize(1792, 1120))
    virtual = _macos.CGRect(_macos.CGPoint(1792, 0), _macos.CGSize(1280, 720))
    runtime.cg.CGDisplayBounds.side_effect = lambda i: physical if i == 1 else virtual
    result = _ARRANGE(runtime, 42)
    assert result.origin.x == 1792
    assert runtime.cg.CGConfigureDisplayOrigin.call_args.args[1:] == (42, 1792, 0)
    assert runtime.cg.CGConfigureDisplayMirrorOfDisplay.call_args.args[1:] == (42, 0)
    runtime.cg.CGCancelDisplayConfiguration.assert_not_called()


def test_arrangement_cancels_a_failed_configuration(runtime):
    runtime.cg.CGBeginDisplayConfiguration.return_value = 0
    runtime.displays.return_value = []
    runtime.cg.CGConfigureDisplayMirrorOfDisplay.return_value = 1006
    with pytest.raises(WebSkrapError, match="could not arrange"):
        _ARRANGE(runtime, 42)
    runtime.cg.CGCancelDisplayConfiguration.assert_called_once()
    runtime.cg.CGCompleteDisplayConfiguration.assert_not_called()


async def test_cdp_placement_retries_ignored_launch_coordinates_and_reads_native_screen(
    monkeypatch,
):
    connection = Mock()
    seen = iter([[1792, 1120, 0, 0, 1000, 700], [1280, 720, 1792, 25, 1000, 650]])

    async def send(method, params=None, **kwargs):
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": "tab"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": "session"}
        if method == "Browser.getWindowForTarget":
            return {"windowId": 7}
        return {}

    connection.send = AsyncMock(side_effect=send)
    evaluate = AsyncMock(side_effect=lambda *_: next(seen))
    monkeypatch.setattr(_macos.CDPPage, "evaluate", evaluate)
    await _macos.place_browser(
        connection, (1792, 0), 1280, 720, ["--window-position=1800,25", "--window-size=1000,650"]
    )
    placements = [
        call.args[1]["bounds"]
        for call in connection.send.call_args_list
        if call.args[0] == "Browser.setWindowBounds"
    ]
    assert placements[0] == {"windowState": "normal"}
    assert placements[1:] == [{"left": 1800, "top": 25, "width": 1000, "height": 650}] * 2
    assert evaluate.await_count == 2
    assert connection.send.call_args.args[0] == "Target.detachFromTarget"


@pytest.mark.parametrize("native_screen", [[1792, 1120], [1280, 720]])
async def test_cdp_placement_rejects_a_browser_left_on_the_physical_screen(
    monkeypatch, native_screen
):
    connection = Mock()
    connection.send = AsyncMock(
        side_effect=[
            {"targetInfos": [{"type": "page", "targetId": "tab"}]},
            {"sessionId": "session"},
            {"windowId": 7},
            {},
            {},
            {},
        ]
    )
    monkeypatch.setattr(
        _macos.CDPPage, "evaluate", AsyncMock(return_value=[*native_screen, 0, 0, 1000, 700])
    )
    ticks = iter([0, 0, 0, 11])
    monkeypatch.setattr(_macos, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with pytest.raises(WebSkrapError, match="Chrome stayed on the wrong screen"):
        await _macos.place_browser(connection, (1792, 0), 1280, 720, [])
    assert connection.send.call_args.args[0] == "Target.detachFromTarget"


def test_arrangement_does_not_cancel_an_already_completed_configuration(runtime):
    runtime.cg.CGBeginDisplayConfiguration.return_value = 0
    runtime.displays.return_value = []
    runtime.cg.CGConfigureDisplayMirrorOfDisplay.return_value = 0
    runtime.cg.CGConfigureDisplayOrigin.return_value = 0
    runtime.cg.CGCompleteDisplayConfiguration.return_value = 1006
    with pytest.raises(WebSkrapError, match="could not apply display arrangement"):
        _ARRANGE(runtime, 42)
    runtime.cg.CGCancelDisplayConfiguration.assert_not_called()
