"""Cloudflare Turnstile checkbox: find it and click it the way a person would.

Opt-in through ``solve_cloudflare``; :func:`webskrap.challenges.settle` drives
it while Cloudflare's challenge page is on screen.

Cloudflare's "Just a moment" page draws its checkbox in a cross-site iframe
from ``challenges.cloudflare.com``, under a closed shadow root that no page
script can reach. CDP's pierced DOM can, without running anything in the
page: the checkbox is the frame's ``input[type=checkbox]``, measured again
right before every click. The input spans the whole label; the square a
person aims at is its left end, as wide as the input is tall. An input hidden
or styled down to nothing is measured through its parent instead. While the
widget verifies, neither has a box and nothing is clicked.

The gesture is the one that passed in live runs: the cursor wanders for
:data:`WANDER_S` seconds, follows the Bezier path of
:func:`webskrap.cdp_dom.travel`, pauses, and holds the button like a mouse.
A click that does not clear the page is repeated after
:data:`CLICK_INTERVAL_S`, at most :data:`MAX_CLICKS` times, all within
:data:`TIMEOUT_S` of the first sighting.

A click only answers the widget. Cloudflare still scores the browser, the exit
IP and the gesture, and can refuse all three. No token is read, copied or
injected, no outside solving service is used, and other CAPTCHAs are never
touched.

Only out-of-process frames are searched. Chromium isolates sites by default,
so the cross-site widget gets its own process unless a launch flag turns site
isolation off.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from webskrap import cdp_dom
from webskrap.cdp import CDPPage
from webskrap.errors import WebSkrapError
from webskrap.human import Box, click_point, uniform

FRAME_HOST = "challenges.cloudflare.com"
TIMEOUT_S = 60.0
CLICK_INTERVAL_S = 20.0
MAX_CLICKS = 3
WANDER_S = (1.5, 3.0)
_MIN_SIDE = 4.0


@dataclass
class Checkbox:
    """Clicks on one checkbox challenge.

    :meth:`step` is polled until the page clears. ``seen`` turns True once the
    checkbox has been drawn, which marks the challenge as a checkbox one.
    """

    page: CDPPage
    deadline: float = field(default_factory=lambda: time.monotonic() + TIMEOUT_S)
    clicks: int = 0
    next_click: float = 0.0
    seen: bool = False

    async def step(self) -> None:
        """Click the checkbox when it is drawn and a click is due; else do nothing.

        A gesture cut short by a navigation still counts as a click: the page
        usually navigated because it cleared, and counting it keeps the number
        of attempts bounded.
        """
        now = time.monotonic()
        if self.clicks >= MAX_CLICKS or now < self.next_click or now >= self.deadline:
            return
        box = await locate(self.page)
        if box is None:
            return
        self.seen = True
        with suppress(WebSkrapError):
            if self.clicks == 0:
                await _wander(self.page, uniform(*WANDER_S))
                box = await locate(self.page)
                if box is None:
                    return
            await _click(self.page, box)
        self.clicks += 1
        self.next_click = time.monotonic() + CLICK_INTERVAL_S


async def locate(page: CDPPage) -> Box | None:
    """Return the drawn checkbox square in ``page``'s viewport, or None."""
    try:
        frames = await cdp_dom.frames(page)
    except WebSkrapError:
        return None
    for frame in frames:
        if (
            frame.owner is None
            or frame.page.session_id == page.session_id
            or urlsplit(frame.url).hostname != FRAME_HOST
        ):
            continue
        box = await _square_in(frame.page)
        if box is not None:
            return {**box, "x": box["x"] + frame.offset_x, "y": box["y"] + frame.offset_y}
    return None


async def _square_in(frame: CDPPage) -> Box | None:
    """Return the checkbox square in ``frame``'s own viewport, or None."""
    try:
        document = await frame.send("DOM.getDocument", {"depth": -1, "pierce": True})
    except WebSkrapError:
        return None
    for node_id in find_checkbox(document["root"]):
        try:
            model = await frame.send("DOM.getBoxModel", {"nodeId": node_id})
        except WebSkrapError:
            continue
        box = square(model["model"]["content"])
        if box is not None:
            return box
    return None


def find_checkbox(root: Mapping[str, Any]) -> list[int]:
    """Return node ids to measure: the first checkbox input, then its parent.

    Walks children and shadow roots in document order, including closed ones.
    """
    stack: list[tuple[Mapping[str, Any], int | None]] = [(root, None)]
    while stack:
        node, parent = stack.pop()
        attributes = node.get("attributes", [])
        names = dict(zip(attributes[::2], attributes[1::2], strict=False))
        if node.get("nodeName") == "INPUT" and names.get("type") == "checkbox":
            return [node["nodeId"]] + ([parent] if parent is not None else [])
        children = [*node.get("shadowRoots", []), *node.get("children", [])]
        stack.extend((child, node.get("nodeId")) for child in reversed(children))
    return []


def square(quad: list[float]) -> Box | None:
    """Return the left square of a content quad, or None when it is too small."""
    x, y = quad[0], quad[1]
    width, height = quad[2] - x, quad[5] - y
    if width < _MIN_SIDE or height < _MIN_SIDE:
        return None
    return {"x": x, "y": y, "width": min(width, height), "height": height}


async def _wander(page: CDPPage, seconds: float) -> None:
    """Move the cursor between random viewport points for ``seconds``."""
    metrics = await page.send("Page.getLayoutMetrics")
    width = metrics["cssLayoutViewport"]["clientWidth"]
    height = metrics["cssLayoutViewport"]["clientHeight"]
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        x = uniform(40, max(41, width - 40))
        y = uniform(40, max(41, height - 40))
        await cdp_dom.travel(page, x, y)
        await asyncio.sleep(uniform(0.3, 1.0))


async def _click(page: CDPPage, box: Box) -> None:
    """Travel to ``box`` and click it with a mouse-like hold."""
    x, y = click_point(box, None)
    await cdp_dom.travel(page, x, y)
    await asyncio.sleep(uniform(0.2, 0.5))
    press = {"x": x, "y": y, "button": "left", "clickCount": 1}
    await page.send(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "buttons": 1, "force": cdp_dom.MOUSE_PRESSED_FORCE, **press},
    )
    await asyncio.sleep(uniform(60, 140) / 1000)
    await page.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **press})
