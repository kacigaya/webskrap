"""The macOS virtual display backend: a CoreGraphics ``CGVirtualDisplay``.

``CGVirtualDisplay`` is the private CoreGraphics class behind DeskPad and
Chromium's own display tests (``ui/display/mac/test/virtual_display_util_mac.mm``),
whose usage this follows: a non-zero vendor ID and a serial number unique per
display (macOS 14 requires both), one mode at the requested size, no HiDPI.
It is called through the Objective-C runtime with :mod:`ctypes`, so there is
no PyObjC dependency.

The display exists while the creating process holds it, and macOS removes it
when that process exits, even after a crash. It needs a logged-in GUI
session. Unlike Xvfb it is part of the user's desktop: other apps with screen
recording permission can capture it, and a person at the Mac sees it in
Displays settings.
"""

from __future__ import annotations

import asyncio
import ctypes
import secrets
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from ctypes import c_bool, c_double, c_int32, c_uint32, c_void_p
from pathlib import Path
from typing import Any

from webskrap.cdp import CDPConnection, CDPPage
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.paths import secure_directory

OBJC_PATH = "/usr/lib/libobjc.A.dylib"
SYSTEM_PATH = "/usr/lib/libSystem.B.dylib"
CORE_GRAPHICS_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
CORE_FOUNDATION_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
FOUNDATION_PATH = "/System/Library/Frameworks/Foundation.framework/Foundation"
DISPLAY_NAME = "WebSkrap Display"
VENDOR_ID = 505
PIXELS_PER_INCH = 96
READY_TIMEOUT_S = 10.0
_DISPATCH_QUEUE_PRIORITY_HIGH = 2


class CGPoint(ctypes.Structure):
    """CoreGraphics ``CGPoint``."""

    _fields_ = (("x", c_double), ("y", c_double))


class CGSize(ctypes.Structure):
    """CoreGraphics ``CGSize``."""

    _fields_ = (("width", c_double), ("height", c_double))


class CGRect(ctypes.Structure):
    """CoreGraphics ``CGRect``."""

    _fields_ = (("origin", CGPoint), ("size", CGSize))


def _fail(message: str) -> WebSkrapError:
    return WebSkrapError(f"virtual_display on macOS: {message}", code=ErrorCode.BROWSER_LAUNCH)


class _Runtime:
    """The Objective-C runtime and the CoreGraphics calls this backend makes."""

    def __init__(self) -> None:
        self.objc = ctypes.CDLL(OBJC_PATH)
        self.system = ctypes.CDLL(SYSTEM_PATH)
        self.cg = ctypes.CDLL(CORE_GRAPHICS_PATH)
        self.cf = ctypes.CDLL(CORE_FOUNDATION_PATH)
        ctypes.CDLL(FOUNDATION_PATH)
        self.objc.objc_getClass.restype = c_void_p
        self.objc.objc_getClass.argtypes = (ctypes.c_char_p,)
        self.objc.sel_registerName.restype = c_void_p
        self.objc.sel_registerName.argtypes = (ctypes.c_char_p,)
        self.objc.objc_autoreleasePoolPush.restype = c_void_p
        self.objc.objc_autoreleasePoolPop.argtypes = (c_void_p,)
        self.system.dispatch_get_global_queue.restype = c_void_p
        self.system.dispatch_get_global_queue.argtypes = (ctypes.c_long, ctypes.c_ulong)
        self.cg.CGDisplayBounds.restype = CGRect
        self.cg.CGDisplayBounds.argtypes = (c_uint32,)
        self.cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
        self.cg.CGDisplayPixelsWide.argtypes = (c_uint32,)
        self.cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
        self.cg.CGDisplayPixelsHigh.argtypes = (c_uint32,)
        self.cg.CGGetOnlineDisplayList.restype = c_int32
        self.cg.CGGetOnlineDisplayList.argtypes = (
            c_uint32,
            ctypes.POINTER(c_uint32),
            ctypes.POINTER(c_uint32),
        )
        self.cg.CGBeginDisplayConfiguration.restype = c_int32
        self.cg.CGBeginDisplayConfiguration.argtypes = (ctypes.POINTER(c_void_p),)
        self.cg.CGConfigureDisplayOrigin.restype = c_int32
        self.cg.CGConfigureDisplayOrigin.argtypes = (c_void_p, c_uint32, c_int32, c_int32)
        self.cg.CGConfigureDisplayMirrorOfDisplay.restype = c_int32
        self.cg.CGConfigureDisplayMirrorOfDisplay.argtypes = (c_void_p, c_uint32, c_uint32)
        self.cg.CGCompleteDisplayConfiguration.restype = c_int32
        self.cg.CGCompleteDisplayConfiguration.argtypes = (c_void_p, c_uint32)
        self.cg.CGCancelDisplayConfiguration.argtypes = (c_void_p,)
        self.cg.CGSessionCopyCurrentDictionary.restype = c_void_p
        self.cf.CFRelease.argtypes = (c_void_p,)
        self.cf.CFRunLoopRunInMode.restype = c_int32
        self.cf.CFRunLoopRunInMode.argtypes = (c_void_p, c_double, c_bool)
        self.default_mode = c_void_p.in_dll(self.cf, "kCFRunLoopDefaultMode").value
        address = ctypes.cast(self.objc.objc_msgSend, c_void_p).value
        if address is None:
            raise ValueError("objc_msgSend is unavailable")
        self._msg_send = address

    def cls(self, name: str) -> int | None:
        return self.objc.objc_getClass(name.encode())

    def send(
        self,
        receiver: int | None,
        selector: str,
        *args: Any,
        restype: Any = c_void_p,
        argtypes: tuple[Any, ...] = (),
    ) -> Any:
        prototype = ctypes.CFUNCTYPE(restype, c_void_p, c_void_p, *argtypes)
        function = prototype(self._msg_send)
        return function(receiver, self.objc.sel_registerName(selector.encode()), *args)

    def responds(self, receiver: int | None, selector: str) -> bool:
        return bool(
            self.send(
                receiver,
                "respondsToSelector:",
                self.objc.sel_registerName(selector.encode()),
                restype=c_bool,
                argtypes=(c_void_p,),
            )
        )

    def new(self, class_name: str) -> int | None:
        return self.send(self.send(self.cls(class_name), "alloc"), "init")

    def gui_session(self) -> bool:
        session = self.cg.CGSessionCopyCurrentDictionary()
        if not session:
            return False
        self.cf.CFRelease(session)
        return True

    def displays(self) -> list[int]:
        ids = (c_uint32 * 32)()
        count = c_uint32()
        if self.cg.CGGetOnlineDisplayList(32, ids, ctypes.byref(count)) != 0:
            raise _fail("could not enumerate online displays")
        return list(ids[: count.value])

    def online(self, display_id: int) -> bool:
        return display_id in self.displays()

    def pump(self, seconds: float) -> None:
        self.cf.CFRunLoopRunInMode(self.default_mode, seconds, False)


def available() -> bool:
    """True on a macOS whose CoreGraphics has ``CGVirtualDisplay``. Never raises."""
    try:
        return bool(_Runtime().cls("CGVirtualDisplay"))
    except (OSError, AttributeError, ValueError):
        return False


def _create(runtime: _Runtime, width: int, height: int) -> int:
    descriptor = runtime.new("CGVirtualDisplayDescriptor")
    if not descriptor:
        raise _fail("this macOS has no CGVirtualDisplay (macOS 11 or later is needed)")
    try:
        name = runtime.send(
            runtime.cls("NSString"),
            "stringWithUTF8String:",
            DISPLAY_NAME.encode(),
            argtypes=(ctypes.c_char_p,),
        )
        queue = runtime.system.dispatch_get_global_queue(_DISPATCH_QUEUE_PRIORITY_HIGH, 0)
        serial = 1 + secrets.randbelow(0x7FFFFFFE)
        millimeters = CGSize(25.4 * width / PIXELS_PER_INCH, 25.4 * height / PIXELS_PER_INCH)
        runtime.send(descriptor, "setQueue:", queue, argtypes=(c_void_p,))
        runtime.send(descriptor, "setName:", name, argtypes=(c_void_p,))
        for selector, value in (
            ("setMaxPixelsWide:", width),
            ("setMaxPixelsHigh:", height),
            ("setVendorID:", VENDOR_ID),
            ("setProductID:", 0),
            ("setSerialNum:", serial),
            ("setSerialNumber:", serial),
        ):
            if runtime.responds(descriptor, selector):
                runtime.send(descriptor, selector, value, argtypes=(c_uint32,))
        runtime.send(descriptor, "setSizeInMillimeters:", millimeters, argtypes=(CGSize,))
        for selector, x, y in (
            ("setWhitePoint:", 0.3125, 0.3291),
            ("setBluePrimary:", 0.1494, 0.0557),
            ("setGreenPrimary:", 0.2559, 0.6983),
            ("setRedPrimary:", 0.6797, 0.3203),
        ):
            runtime.send(descriptor, selector, CGPoint(x, y), argtypes=(CGPoint,))
        virtual = runtime.send(
            runtime.send(runtime.cls("CGVirtualDisplay"), "alloc"),
            "initWithDescriptor:",
            descriptor,
            argtypes=(c_void_p,),
        )
    finally:
        runtime.send(descriptor, "release")
    if not virtual:
        raise _fail("macOS refused to create a CGVirtualDisplay")
    try:
        _apply_mode(runtime, virtual, width, height)
    except BaseException:
        runtime.send(virtual, "release")
        raise
    return virtual


def _apply_mode(runtime: _Runtime, virtual: int, width: int, height: int) -> None:
    settings = runtime.new("CGVirtualDisplaySettings")
    mode = runtime.send(
        runtime.send(runtime.cls("CGVirtualDisplayMode"), "alloc"),
        "initWithWidth:height:refreshRate:",
        width,
        height,
        60.0,
        argtypes=(c_uint32, c_uint32, c_double),
    )
    try:
        modes = runtime.send(runtime.cls("NSArray"), "arrayWithObject:", mode, argtypes=(c_void_p,))
        runtime.send(settings, "setHiDPI:", 0, argtypes=(c_uint32,))
        runtime.send(settings, "setModes:", modes, argtypes=(c_void_p,))
        if not runtime.send(
            virtual, "applySettings:", settings, restype=c_bool, argtypes=(c_void_p,)
        ):
            raise _fail(f"macOS refused the {width}x{height} mode")
    finally:
        runtime.send(mode, "release")
        runtime.send(settings, "release")


def _wait_until_online(runtime: _Runtime, display_id: int, width: int, height: int) -> CGRect:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if (
            runtime.online(display_id)
            and runtime.cg.CGDisplayPixelsWide(display_id) == width
            and runtime.cg.CGDisplayPixelsHigh(display_id) == height
        ):
            return runtime.cg.CGDisplayBounds(display_id)
        runtime.pump(0.05)
    raise _fail(f"display {display_id} did not come online at {width}x{height}")


@contextmanager
def hold(width: int, height: int) -> Iterator[tuple[int, int]]:
    """Create a ``width`` x ``height`` display and yield its top-left corner.

    The corner is in global desktop points, which ``--window-position``
    takes. The display is removed on exit, and by macOS if this process dies.

    Raises:
        WebSkrapError: ``browser_launch`` outside a GUI session, on a macOS
            without the API, or if the display does not come online.
    """
    if width <= 0 or height <= 0:
        raise _fail("width and height must be positive")
    try:
        runtime = _Runtime()
    except (OSError, AttributeError, ValueError) as exc:
        raise _fail(f"CoreGraphics could not be loaded ({exc})") from None
    if not runtime.gui_session():
        raise _fail("no logged-in GUI session; run inside the user's desktop session")
    pool = runtime.objc.objc_autoreleasePoolPush()
    virtual = None
    try:
        virtual = _create(runtime, width, height)
        display_id = runtime.send(virtual, "displayID", restype=c_uint32)
        if not display_id:
            raise _fail("macOS created a display without an ID")
        _wait_until_online(runtime, display_id, width, height)
        with _layout_lock():
            bounds = _arrange(runtime, display_id)
        yield round(bounds.origin.x), round(bounds.origin.y)
    finally:
        if virtual:
            with suppress(Exception):
                runtime.send(virtual, "release")
        runtime.objc.objc_autoreleasePoolPop(pool)


@contextmanager
def _layout_lock() -> Iterator[None]:
    if sys.platform == "win32":
        raise _fail("display arrangement requires macOS")
    import fcntl

    directory = Path.home() / ".webskrap" / "display-locks"
    secure_directory(directory)
    with (directory / "macos-layout.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _arrange(runtime: _Runtime, display_id: int) -> CGRect:
    """Extend the desktop to the right in logical points, without changing other displays."""
    configuration = c_void_p()
    status = runtime.cg.CGBeginDisplayConfiguration(ctypes.byref(configuration))
    if status:
        raise _fail(f"could not begin display arrangement ({status})")
    try:
        others = [runtime.cg.CGDisplayBounds(i) for i in runtime.displays() if i != display_id]
        right = round(max((b.origin.x + b.size.width for b in others), default=0))
        for operation, arguments in (
            (runtime.cg.CGConfigureDisplayMirrorOfDisplay, (configuration, display_id, 0)),
            (runtime.cg.CGConfigureDisplayOrigin, (configuration, display_id, right, 0)),
        ):
            status = operation(*arguments)
            if status:
                raise _fail(f"could not arrange the virtual display ({status})")
    except BaseException:
        runtime.cg.CGCancelDisplayConfiguration(configuration)
        raise
    status = runtime.cg.CGCompleteDisplayConfiguration(configuration, 0)
    if status:
        raise _fail(f"could not apply display arrangement ({status})")
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        bounds = runtime.cg.CGDisplayBounds(display_id)
        if round(bounds.origin.x) == right and round(bounds.origin.y) == 0:
            return bounds
        runtime.pump(0.05)
    raise _fail("the virtual display did not reach its requested desktop origin")


async def place_browser(
    connection: CDPConnection,
    origin: tuple[int, int],
    width: int,
    height: int,
    args: list[str],
) -> None:
    """Move the real Chrome window and verify native screen assignment before use."""
    bounds = {"left": origin[0], "top": origin[1], "width": width, "height": height}
    for flag, fields in (
        ("--window-position=", ("left", "top")),
        ("--window-size=", ("width", "height")),
    ):
        for arg in args:
            if arg.startswith(flag):
                try:
                    values = [int(v) for v in arg[len(flag) :].split(",")]
                    if len(values) != 2:
                        raise ValueError
                except ValueError:
                    raise _fail(f"invalid window bounds: {arg}") from None
                bounds.update(zip(fields, values, strict=True))
    deadline = time.monotonic() + READY_TIMEOUT_S
    target = None
    while time.monotonic() < deadline:
        targets = await connection.send("Target.getTargets")
        target = next((t for t in targets["targetInfos"] if t["type"] == "page"), None)
        if target is not None:
            break
        await asyncio.sleep(0.1)
    if target is None:
        raise _fail("Chrome has no page window to place on the virtual display")
    attached = await connection.send(
        "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
    )
    page = CDPPage(connection, attached["sessionId"], target["targetId"])
    try:
        window = await connection.send("Browser.getWindowForTarget", {"targetId": page.target_id})
        window_id = window["windowId"]
        await connection.send(
            "Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}}
        )
        seen = None
        while time.monotonic() < deadline:
            await connection.send(
                "Browser.setWindowBounds", {"windowId": window_id, "bounds": bounds}
            )
            seen = await page.evaluate(
                "() => [screen.width, screen.height, screenX, screenY, outerWidth, outerHeight]"
            )
            if (
                seen[:2] == [width, height]
                and origin[0] <= seen[2] + seen[4] / 2 < origin[0] + width
                and origin[1] <= seen[3] + seen[5] / 2 < origin[1] + height
            ):
                return
            await asyncio.sleep(0.1)
        raise _fail(
            f"Chrome stayed on the wrong screen (observed {seen}, expected {width}x{height})"
        )
    finally:
        await connection.send("Target.detachFromTarget", {"sessionId": attached["sessionId"]})
