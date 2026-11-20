"""Page actions for the CDP driver."""

from __future__ import annotations

import asyncio
import base64
import struct
import time
from collections.abc import Awaitable
from contextlib import suppress
from pathlib import Path
from typing import Any, TypeVar

from webskrap.config.models import ElementState, LoadState
from webskrap.driver import cdp_dom
from webskrap.driver.cdp import CDPError, CDPPage
from webskrap.driver.polling import _POLL_S, _within, poll
from webskrap.driver.view_types import BrowserView, Viewport
from webskrap.errors import ErrorCode, WebSkrapError

T = TypeVar("T")

_STATE_SCRIPT = "({ url: location.href, title: document.title })"
_VIEWPORT_SCRIPT = (
    "({ width: innerWidth, height: innerHeight, scroll_x: scrollX, scroll_y: scrollY, "
    "dpr: devicePixelRatio })"
)
# Same window as browser_session.read_text: code points, not UTF-16 units.
_TEXT_SCRIPT = """({limit, start}) => {
  const text = document.body ? document.body.innerText : '';
  const end = start + limit;
  const selected = [];
  let length = 0;
  for (const character of text) {
    if (length >= start && length < end) selected.push(character);
    length++;
  }
  const offset = Math.min(start, length);
  const stop = Math.min(end, length);
  return {
    title: document.title,
    text: selected.join(''),
    text_length: length,
    text_offset: offset,
    text_truncated: stop < length,
    next_text_offset: stop < length ? stop : null,
  };
}"""
# Mirrors Playwright's get_by_text default: case-insensitive substring of
# whitespace-normalized text in a visible element.
_TEXT_VISIBLE_SCRIPT = """({needle}) => {
  const want = needle.replace(/\\s+/g, ' ').trim().toLowerCase();
  const walker = document.createTreeWalker(document.body || document, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const element = node.parentElement;
    if (!element) continue;
    const text = (element.innerText || element.textContent || '')
      .replace(/\\s+/g, ' ').trim().toLowerCase();
    if (!text.includes(want)) continue;
    const box = element.getBoundingClientRect();
    if (box.width > 0 && box.height > 0 && getComputedStyle(element).visibility !== 'hidden')
      return true;
  }
  return false;
}"""
# Serializes an eval result the way Patchright's evaluate reports it: windows
# and DOM nodes become "ref: <Window>" / "ref: <Node>" placeholders, and a
# reference back into the object's own path becomes "[Circular]" (Patchright
# rebuilds the cycle; JSON cannot carry one). A function value is called
# first, as Playwright calls an expression that evaluates to a function.
_SERIALIZE_FUNCTION = """async function () {
  const value = typeof this === 'function' ? await this() : this;
  const path = new Set();
  const walk = (item) => {
    if (item === undefined || typeof item === 'function' || typeof item === 'symbol') return null;
    if (typeof item === 'bigint') return String(item);
    if (typeof item === 'number' && !Number.isFinite(item)) return String(item);
    if (item === null || typeof item !== 'object') return item;
    if (typeof Window !== 'undefined' && item instanceof Window) return 'ref: <Window>';
    if (typeof Node !== 'undefined' && item instanceof Node) return 'ref: <Node>';
    if (item instanceof Date) return item.toJSON();
    if (path.has(item)) return '[Circular]';
    path.add(item);
    let out;
    if (Array.isArray(item)) out = item.map(walk);
    else {
      out = {};
      for (const key of Object.keys(item)) out[key] = walk(item[key]);
    }
    path.delete(item);
    return out;
  };
  return walk(value);
}"""
#: CDP ``unserializableValue`` strings for primitives JSON cannot carry.
_UNSERIALIZABLE: dict[str, float] = {
    "NaN": float("nan"),
    "Infinity": float("inf"),
    "-Infinity": float("-inf"),
    "-0": -0.0,
}
# networkidle has no live event to subscribe to after the fact; this quiet
# window matches the 500 ms Chromium's own networkIdle lifecycle event uses.
_NETWORK_QUIET_S = 0.5


async def page_state(page: CDPPage) -> dict[str, Any]:
    """Return the page's current ``{"url", "title"}``."""
    state = await page.evaluate(_STATE_SCRIPT)
    return {"url": state["url"], "title": state["title"]}


async def history(page: CDPPage, method: str) -> dict[str, Any]:
    """Run ``go_back``, ``go_forward`` or ``reload`` and wait for the load.

    Like Playwright, going past either end of the history does nothing. The
    session browser runs without the back/forward cache, so a move to another
    document always fires a load event; a move between entries of the same
    document (``#anchor`` links, ``pushState``) is done once Chromium reports
    it, since no load follows.

    Raises:
        WebSkrapError: ``timeout`` when neither signal arrives in time.
    """
    await page.enable_events()
    events = page.record("Page.lifecycleEvent", "Page.navigatedWithinDocument")
    try:
        if method == "reload":
            await page.send("Page.reload")
        else:
            entries = await page.send("Page.getNavigationHistory")
            index = entries["currentIndex"] + (-1 if method == "go_back" else 1)
            if not 0 <= index < len(entries["entries"]):
                return await page_state(page)
            await page.send(
                "Page.navigateToHistoryEntry", {"entryId": entries["entries"][index]["id"]}
            )
        # Iframes report lifecycle and in-document navigations too; only the
        # main frame (whose id is the target id) ends the command.
        await events.wait(
            lambda event, params: (
                params.get("frameId") == page.target_id
                and (
                    event == "Page.navigatedWithinDocument"
                    or (event == "Page.lifecycleEvent" and params.get("name") == "load")
                )
            ),
            timeout_s=page.default_timeout_s,
            what=f"{method} to finish",
        )
    finally:
        events.stop()
    return await page_state(page)


async def read_text(page: CDPPage, *, max_chars: int, offset: int = 0) -> dict[str, Any]:
    """Return a visible-text window; see :func:`webskrap.browser.actions.read_text`."""
    window = await page.evaluate(
        _TEXT_SCRIPT, {"limit": max(0, max_chars), "start": max(0, offset)}
    )
    state = await page_state(page)
    return {"url": state["url"], **window}


async def body_text(page: CDPPage, *, max_chars: int) -> dict[str, Any]:
    """Return ``{"url", "title", "text", "text_length"}`` with text cut at ``max_chars``."""
    state = await page.evaluate(
        "(limit) => { const text = document.body ? document.body.innerText : '';"
        " return { url: location.href, title: document.title,"
        " text: text.slice(0, limit), text_length: text.length }; }",
        max(0, max_chars),
    )
    return dict(state)


async def evaluate(page: CDPPage, expression: str) -> Any:
    """Evaluate ``expression`` in the page's own world, as Patchright's eval does.

    ``Runtime.evaluate`` without a context id runs in the main world and does
    not need ``Runtime.enable``. A function value is then called with no
    argument, matching Playwright, and objects are serialized as Patchright
    does (see :data:`_SERIALIZE_FUNCTION`). Nothing is wrapped around the
    caller's code itself.

    Raises:
        CDPError: If the expression throws or its value cannot be serialized.
    """
    result = await page.send(
        "Runtime.evaluate",
        # Playwright evaluates with a user gesture, so eval can open a popup or
        # start media the way a page action would.
        {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": False,
            "userGesture": True,
        },
    )
    _raise_for_exception(result)
    remote = result["result"]
    object_id = remote.get("objectId")
    if object_id is None:
        unserializable = remote.get("unserializableValue")
        if unserializable is not None:
            if unserializable.endswith("n"):
                return int(unserializable[:-1])
            return _UNSERIALIZABLE.get(unserializable)
        return remote.get("value")
    try:
        value = await page.send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": _SERIALIZE_FUNCTION,
                "objectId": object_id,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
        )
        _raise_for_exception(value)
        return value["result"].get("value")
    finally:
        with suppress(WebSkrapError):
            await page.send("Runtime.releaseObject", {"objectId": object_id})


def _raise_for_exception(result: dict[str, Any]) -> None:
    if "exceptionDetails" in result:
        details = result["exceptionDetails"]
        description = details.get("exception", {}).get("description") or details.get("text")
        raise CDPError(f"page script failed: {description}")


async def viewport(page: CDPPage) -> Viewport:
    """Return viewport size and scroll offset in CSS pixels."""
    geometry = await page.evaluate(_VIEWPORT_SCRIPT)
    return {
        "width": int(geometry["width"]),
        "height": int(geometry["height"]),
        "scroll_x": float(geometry["scroll_x"]),
        "scroll_y": float(geometry["scroll_y"]),
    }


async def view(page: CDPPage) -> BrowserView:
    """Capture the viewport at one image pixel per CSS pixel.

    On a high-density screen the capture is scaled down by the device pixel
    ratio, so mouse coordinates read off the image are CSS pixels.
    """
    geometry = await page.evaluate(_VIEWPORT_SCRIPT)
    params: dict[str, Any] = {"format": "png"}
    if geometry["dpr"] != 1:
        params["clip"] = {
            "x": geometry["scroll_x"],
            "y": geometry["scroll_y"],
            "width": geometry["width"],
            "height": geometry["height"],
            "scale": 1 / geometry["dpr"],
        }
    shot = await page.send("Page.captureScreenshot", params)
    image = base64.b64decode(shot["data"])
    width, height = struct.unpack(">II", image[16:24])
    state = await page_state(page)
    return BrowserView(
        image=image,
        url=state["url"],
        title=state["title"],
        width=width,
        height=height,
        scroll_x=float(geometry["scroll_x"]),
        scroll_y=float(geometry["scroll_y"]),
    )


async def screenshot(
    page: CDPPage, path: Path, *, full_page: bool, target: str | None = None
) -> dict[str, float] | None:
    """Write a PNG of the viewport, the whole page, or one element to ``path``.

    Returns the element's clip when ``target`` is set, else None.
    """
    clip = await cdp_dom.element_clip(page, target) if target is not None else None
    image = await page.screenshot(full_page=full_page, clip=clip)
    await asyncio.to_thread(path.write_bytes, image)
    return clip


async def insert_text(page: CDPPage, text: str) -> dict[str, Any]:
    """Insert literal text into the focused control without key events."""
    await page.send("Input.insertText", {"text": text})
    return await page_state(page)


_BUTTON_BITS = {"left": 1, "right": 2, "middle": 4}


async def _mouse(
    page: CDPPage, kind: str, x: float, y: float, *, button: str = "none", **extra: Any
) -> None:
    if extra.get("buttons"):
        extra.setdefault("force", cdp_dom.MOUSE_PRESSED_FORCE)
    await page.send(
        "Input.dispatchMouseEvent",
        {"type": kind, "x": x, "y": y, "button": button, **extra},
    )
    page.pointer = (x, y)


async def mouse_action(
    page: CDPPage,
    action: str,
    x: float,
    y: float,
    *,
    end_x: float | None = None,
    end_y: float | None = None,
    delta_x: float = 0,
    delta_y: float = 0,
    button: str = "left",
) -> dict[str, Any]:
    """Send trusted mouse input; arguments match :func:`browser_session.mouse_action`.

    The caller has validated the arguments and the viewport bounds.
    """
    pressed = _BUTTON_BITS.get(button, 1)
    # The cursor travels from where it last was instead of jumping.
    await cdp_dom.travel(page, x, y)
    if action in {"click", "dblclick"}:
        for click_count in (1, 2) if action == "dblclick" else (1,):
            await _mouse(
                page,
                "mousePressed",
                x,
                y,
                button=button,
                buttons=pressed,
                clickCount=click_count,
            )
            await _mouse(page, "mouseReleased", x, y, button=button, clickCount=click_count)
    elif action == "scroll":
        await _mouse(page, "mouseWheel", x, y, deltaX=delta_x, deltaY=delta_y)
    elif action == "drag" and end_x is not None and end_y is not None:
        await _drag(page, x, y, end_x, end_y, button=button, pressed=pressed)
    return await page_state(page)


async def _drag(
    page: CDPPage, x: float, y: float, end_x: float, end_y: float, *, button: str, pressed: int
) -> None:
    """Drag in ten steps, as Playwright's ``mouse.move(steps=10)`` does.

    Starting on a selection, a link or a draggable element begins a native
    drag, after which Chromium swallows raw mouse events, so the page would
    never see the release. As in Playwright, drags are intercepted while the
    button is down: once Chromium reports one, the rest of the gesture is sent
    as ``dragEnter``/``dragOver``/``drop`` events with the drag's data.
    """
    await _mouse(page, "mousePressed", x, y, button=button, buttons=pressed, clickCount=1)
    intercepted = page.record("Input.dragIntercepted")
    await page.send("Input.setInterceptDrags", {"enabled": True})
    data: dict[str, Any] | None = None
    try:
        for step in range(1, 11):
            px = x + (end_x - x) * step / 10
            py = y + (end_y - y) * step / 10
            if data is not None:
                await _drag_event(page, "dragOver", px, py, data)
                continue
            await _mouse(page, "mouseMoved", px, py, button=button, buttons=pressed)
            # The interception event, when any, arrives before the move's reply.
            if intercepted.events:
                started: dict[str, Any] = intercepted.events[-1][1]["data"]
                data = started
                await _drag_event(page, "dragEnter", px, py, started)
    finally:
        intercepted.stop()
        if data is not None:
            await _drag_event(page, "drop", end_x, end_y, data)
        else:
            await _mouse(page, "mouseReleased", end_x, end_y, button=button, clickCount=1)
        await page.send("Input.setInterceptDrags", {"enabled": False})


async def _drag_event(page: CDPPage, kind: str, x: float, y: float, data: dict[str, Any]) -> None:
    await page.send("Input.dispatchDragEvent", {"type": kind, "x": x, "y": y, "data": data})
    page.pointer = (x, y)


async def wait_for(
    page: CDPPage,
    *,
    condition: str,
    text: str | None = None,
    text_gone: str | None = None,
    selector: str | None = None,
    selector_state: ElementState = "visible",
    load_state: LoadState | None = None,
    timeout_ms: float,
) -> dict[str, Any]:
    """Wait for one condition; see :func:`webskrap.browser.actions.wait_for`.

    ``selector`` is a snapshot ref or a CSS selector on this driver.

    Raises:
        WebSkrapError: ``timeout`` when the condition is not met in time, or
            ``usage`` for a selector that is not valid CSS.
    """
    if text is not None:
        needle = text

        async def text_visible() -> bool:
            return bool(await page.evaluate(_TEXT_VISIBLE_SCRIPT, {"needle": needle}))

        await poll(text_visible, timeout_ms, f"text '{text}'")
    if text_gone is not None:
        gone = text_gone

        async def text_hidden() -> bool:
            return not await page.evaluate(_TEXT_VISIBLE_SCRIPT, {"needle": gone})

        await poll(text_hidden, timeout_ms, f"text '{text_gone}' to disappear")
    if selector is not None:
        target = selector
        wanted = selector_state

        async def selector_reached() -> bool:
            state = await cdp_dom.element_state(page, target)
            if wanted == "attached":
                return state != "detached"
            if wanted == "hidden":
                return state != "visible"
            return state == wanted

        await poll(selector_reached, timeout_ms, f"{selector} to be {selector_state}")
    if load_state is not None:
        await _wait_load_state(page, load_state, timeout_ms)
    return {**await page_state(page), "matched": condition}


async def _within_or_timeout(awaitable: Awaitable[T], deadline: float, timeout_ms: float) -> T:
    try:
        return await _within(awaitable, deadline)
    except TimeoutError as exc:
        msg = f"timed out after {timeout_ms:.0f}ms waiting for load state networkidle"
        raise WebSkrapError(msg, code=ErrorCode.TIMEOUT) from exc


async def _wait_load_state(page: CDPPage, load_state: LoadState, timeout_ms: float) -> None:
    """Wait until the current document reached ``load_state``, within ``timeout_ms``.

    ``document.readyState`` answers for the two document states. The live
    ``networkIdle`` lifecycle event usually fired before this connection
    existed, so ``networkidle`` is measured here instead: 500 ms with no
    request in flight that this connection saw start, and no new entry in the
    page's resource timeline (which also covers requests this connection
    could not see begin, once they finish). A request that began before the
    command attached and is still running is invisible until it ends; the
    same holds for Patchright, which also attaches fresh for each command.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    in_flight: set[str] = set()
    network = None
    if load_state == "networkidle":
        await page.enable_events()
        network = page.record(
            "Network.requestWillBeSent", "Network.loadingFinished", "Network.loadingFailed"
        )
    try:
        wanted = "interactive" if load_state == "domcontentloaded" else "complete"

        async def ready() -> bool:
            state = await page.evaluate("document.readyState")
            return state == "complete" or state == wanted

        await poll(ready, max(0.0, deadline - time.monotonic()) * 1000, f"load state {load_state}")
        if network is None:
            return
        count_script = "performance.getEntriesByType('resource').length"
        last = await _within_or_timeout(page.evaluate(count_script), deadline, timeout_ms)
        quiet_since = time.monotonic()
        seen = 0
        while True:
            for method, params in network.events[seen:]:
                request = params.get("requestId", "")
                if method == "Network.requestWillBeSent":
                    in_flight.add(request)
                else:
                    in_flight.discard(request)
            seen = len(network.events)
            current = await _within_or_timeout(page.evaluate(count_script), deadline, timeout_ms)
            if in_flight or current != last:
                last, quiet_since = current, time.monotonic()
            elif time.monotonic() - quiet_since >= _NETWORK_QUIET_S:
                return
            if time.monotonic() >= deadline:
                msg = f"timed out after {timeout_ms:.0f}ms waiting for load state networkidle"
                raise WebSkrapError(msg, code=ErrorCode.TIMEOUT)
            await asyncio.sleep(_POLL_S)
    finally:
        if network is not None:
            network.stop()
