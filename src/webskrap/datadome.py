"""DataDome slider CAPTCHA: find it and drag it the way a person would.

Opt-in through ``solve_datadome``; :func:`webskrap.challenges.settle` drives
it while DataDome's CAPTCHA is on screen.

DataDome draws its CAPTCHA in a cross-site iframe from
``geo.captcha-delivery.com/captcha/``. The handle is ``.slider`` and the slot
it must reach is ``.sliderTarget``, both read through CDP's DOM without
running anything in the page and measured again right before every drag.
A hidden handle has no box, so nothing is dragged while the widget loads.

The gesture: the cursor wanders for :data:`WANDER_S` seconds, travels to just
left of the handle along a Bezier path, grabs it, and drags it to the slot
in :data:`DRAG_STEPS` moves with smoothstep easing, small jitter, and a
slow, faster, slowest rhythm. A drag that does not clear the page is
repeated after :data:`SLIDE_INTERVAL_S`, at most :data:`MAX_SLIDES` times,
all within :data:`TIMEOUT_S` of the first sighting.

Only the slider is handled. DataDome still scores the browser, the exit IP
and the gesture; a hard block (``t=bv``) has no slider and stays blocked.
No cookie or token is read, copied or injected, no outside solving service is
used, and the audio and puzzle-image variants are never touched.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from webskrap import cdp_dom
from webskrap.cdp import CDPPage
from webskrap.errors import WebSkrapError
from webskrap.human import Box, uniform
from webskrap.turnstile import wander

FRAME_HOST = "geo.captcha-delivery.com"
TIMEOUT_S = 60.0
SLIDE_INTERVAL_S = 10.0
MAX_SLIDES = 3
WANDER_S = (3.0, 5.0)
DRAG_STEPS = 35
_MIN_SIDE = 4.0


@dataclass
class Slider:
    """Drags on one slider CAPTCHA.

    :meth:`step` is polled until the page clears. ``seen`` turns True once the
    slider has been drawn, which marks the CAPTCHA as a slider one.
    """

    page: CDPPage
    deadline: float = field(default_factory=lambda: time.monotonic() + TIMEOUT_S)
    slides: int = 0
    next_slide: float = 0.0
    seen: bool = False

    async def step(self) -> None:
        """Drag the slider when it is drawn and a drag is due; else do nothing.

        A gesture cut short by a navigation still counts: the page usually
        navigated because it cleared, and counting it keeps attempts bounded.
        """
        now = time.monotonic()
        if self.slides >= MAX_SLIDES or now < self.next_slide or now >= self.deadline:
            return
        boxes = await locate(self.page)
        if boxes is None:
            return
        self.seen = True
        with suppress(WebSkrapError):
            if self.slides == 0:
                await wander(self.page, uniform(*WANDER_S))
                boxes = await locate(self.page)
                if boxes is None:
                    return
            await _slide(self.page, *boxes)
        self.slides += 1
        self.next_slide = time.monotonic() + SLIDE_INTERVAL_S


async def locate(page: CDPPage) -> tuple[Box, Box] | None:
    """Return the drawn handle and slot in ``page``'s viewport, or None."""
    try:
        frames = await cdp_dom.frames(page)
    except WebSkrapError:
        return None
    for frame in frames:
        url = urlsplit(frame.url)
        if (
            frame.owner is None
            or frame.page.session_id == page.session_id
            or url.hostname != FRAME_HOST
            or not url.path.startswith("/captcha/")
        ):
            continue
        handle = await _box_in(frame.page, ".slider")
        slot = await _box_in(frame.page, ".sliderTarget")
        if handle is None or slot is None:
            continue
        dx, dy = frame.offset_x, frame.offset_y
        return (
            {**handle, "x": handle["x"] + dx, "y": handle["y"] + dy},
            {**slot, "x": slot["x"] + dx, "y": slot["y"] + dy},
        )
    return None


async def _box_in(frame: CDPPage, selector: str) -> Box | None:
    """Return the content box of ``selector`` in ``frame``'s viewport, or None."""
    try:
        document = await frame.send("DOM.getDocument", {"depth": 0})
        found = await frame.send(
            "DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": selector}
        )
        if not found.get("nodeId"):
            return None
        model = await frame.send("DOM.getBoxModel", {"nodeId": found["nodeId"]})
    except WebSkrapError:
        return None
    return box(model["model"]["content"])


def box(quad: list[float]) -> Box | None:
    """Return a content quad as a box, or None when it is too small to drag."""
    width, height = quad[2] - quad[0], quad[5] - quad[1]
    if width < _MIN_SIDE or height < _MIN_SIDE:
        return None
    return {"x": quad[0], "y": quad[1], "width": width, "height": height}


def drag_path(start: tuple[float, float], end_x: float) -> list[tuple[float, float]]:
    """Return the drag's points: smoothstep from ``start`` to ``end_x``, with jitter."""
    x0, y0 = start
    points = []
    for i in range(1, DRAG_STEPS + 1):
        t = i / DRAG_STEPS
        ease = t * t * (3 - 2 * t)
        jitter = round(uniform(-3, 3)) if 5 < i < DRAG_STEPS - 5 else 0
        points.append((round(x0 + (end_x - x0) * ease + jitter), round(y0 + uniform(-2, 2))))
    return points


async def _slide(page: CDPPage, handle: Box, slot: Box) -> None:
    """Grab ``handle`` and drag it onto ``slot`` with a hand-like rhythm."""
    x, y = handle["x"] + 5, handle["y"] + handle["height"] / 2
    await cdp_dom.travel(page, x - 20, y + uniform(-10, 10))
    await asyncio.sleep(uniform(0.2, 0.4))
    await cdp_dom.travel(page, x, y)
    await asyncio.sleep(uniform(0.1, 0.2))
    button = {"button": "left", "clickCount": 1}
    await page.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mousePressed",
            "x": x,
            "y": y,
            "buttons": 1,
            "force": cdp_dom.MOUSE_PRESSED_FORCE,
            **button,
        },
    )
    await asyncio.sleep(uniform(0.05, 0.15))
    path = drag_path((x, y), slot["x"] + slot["width"] / 2)
    for i, (px, py) in enumerate(path, 1):
        await page.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": px, "y": py, "button": "left", "buttons": 1},
        )
        page.pointer = (px, py)
        if i < DRAG_STEPS // 3:
            await asyncio.sleep(uniform(0.04, 0.08))
        elif i < DRAG_STEPS * 2 // 3:
            await asyncio.sleep(uniform(0.02, 0.04))
        else:
            await asyncio.sleep(uniform(0.015, 0.03))
    await asyncio.sleep(uniform(0.1, 0.2))
    x, y = path[-1]
    await page.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, **button})
