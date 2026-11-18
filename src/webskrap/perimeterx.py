"""HUMAN (PerimeterX) Press & Hold: find the button and hold it the way a person would.

Opt-in through ``solve_perimeterx``; :func:`webskrap.challenges.settle` drives
it while HUMAN's challenge is on screen.

HUMAN draws the button in an iframe inside ``#px-captcha``. The iframe has no
cross-site source, so it shares the page's process, and CDP's pierced DOM of
the page reaches it without running anything in the page. The button is the
``role=button`` element there. Hidden decoy frames have no box and are
skipped. The button is measured again right before every hold.

The gesture is the one that passed in live runs: the cursor follows the Bezier
path of :func:`webskrap.cdp_dom.travel` to the button, presses, and holds with
a small hand tremor until the button's progress fill spans it, then lets go
after a short beat. A hold lasts at most :data:`MAX_HOLD_S`. A hold that does
not clear the page is repeated after :data:`HOLD_INTERVAL_S`, at most
:data:`MAX_HOLDS` times, all within :data:`TIMEOUT_S` of the first sighting.

A hold only answers the widget. HUMAN still scores the browser and the exit
IP, and refuses a completed hold from a browser it distrusts. No cookie or
token is read, copied or injected, and no outside solving service is used.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from webskrap import cdp_dom
from webskrap.cdp import CDPPage
from webskrap.errors import WebSkrapError
from webskrap.human import Box, click_point, uniform

CONTAINER_ID = "px-captcha"
TIMEOUT_S = 60.0
HOLD_INTERVAL_S = 8.0
MAX_HOLDS = 3
MAX_HOLD_S = 15.0
_MIN_SIDE = 4.0


@dataclass
class Hold:
    """Holds on one Press & Hold challenge.

    :meth:`step` is polled until the page clears. ``seen`` turns True once the
    button has been drawn, which marks the challenge as a Press & Hold one.
    """

    page: CDPPage
    deadline: float = field(default_factory=lambda: time.monotonic() + TIMEOUT_S)
    holds: int = 0
    next_hold: float = 0.0
    seen: bool = False

    @property
    def attempts(self) -> int:
        """How many gestures have been made so far."""
        return self.holds

    async def step(self) -> None:
        """Hold the button when it is drawn and a hold is due; else do nothing.

        A gesture cut short by a navigation still counts: the page usually
        navigated because it cleared, and counting it keeps attempts bounded.
        """
        now = time.monotonic()
        if self.holds >= MAX_HOLDS or now < self.next_hold or now >= self.deadline:
            return
        found = await measure(self.page)
        if found is None:
            return
        self.seen = True
        with suppress(WebSkrapError):
            await _hold(self.page, *found)
        self.holds += 1
        self.next_hold = time.monotonic() + HOLD_INTERVAL_S


def find_buttons(root: Mapping[str, Any]) -> list[int]:
    """Return the node ids of ``role=button`` elements inside ``#px-captcha``.

    Walks children, shadow roots and same-process frame documents in order.
    """
    found: list[int] = []
    stack: list[tuple[Mapping[str, Any], bool]] = [(root, False)]
    while stack:
        node, inside = stack.pop()
        attributes = node.get("attributes", [])
        names = dict(zip(attributes[::2], attributes[1::2], strict=False))
        inside = inside or names.get("id") == CONTAINER_ID
        if inside and names.get("role") == "button":
            found.append(node["nodeId"])
        children = [*node.get("shadowRoots", []), *node.get("children", [])]
        if "contentDocument" in node:
            children.append(node["contentDocument"])
        stack.extend((child, inside) for child in reversed(children))
    return found


async def measure(page: CDPPage) -> tuple[Box, int] | None:
    """Return the drawn button and how many of its parts span it, or None.

    The progress fill is the part that grows to the button's width, so the
    count goes up by one when a hold is complete.
    """
    try:
        document = await page.send("DOM.getDocument", {"depth": -1, "pierce": True})
        for node_id in find_buttons(document["root"]):
            button = await _box_of(page, node_id)
            if button is None:
                continue
            parts = await page.send("DOM.querySelectorAll", {"nodeId": node_id, "selector": "div"})
            spans = 0
            for part_id in parts["nodeIds"]:
                part = await _box_of(page, part_id)
                if part is not None and part["width"] >= button["width"] - 1:
                    spans += 1
            return button, spans
    except WebSkrapError:
        return None
    return None


async def _box_of(page: CDPPage, node_id: int) -> Box | None:
    """Return the content box of ``node_id`` in ``page``'s viewport, or None."""
    try:
        model = await page.send("DOM.getBoxModel", {"nodeId": node_id})
    except WebSkrapError:
        return None
    return box(model["model"]["content"])


def box(quad: list[float]) -> Box | None:
    """Return a content quad as a box, or None when it is too small to hold."""
    width, height = quad[2] - quad[0], quad[5] - quad[1]
    if width < _MIN_SIDE or height < _MIN_SIDE:
        return None
    return {"x": quad[0], "y": quad[1], "width": width, "height": height}


async def _hold(page: CDPPage, button: Box, spans: int) -> None:
    """Press ``button`` and hold it until its fill completes, with a tremor."""
    x, y = click_point(button, None)
    await cdp_dom.travel(page, x, y)
    await asyncio.sleep(uniform(0.2, 0.4))
    press = {"button": "left", "clickCount": 1}
    await page.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mousePressed",
            "x": x,
            "y": y,
            "buttons": 1,
            "force": cdp_dom.MOUSE_PRESSED_FORCE,
            **press,
        },
    )
    px, py = x, y
    try:
        end = time.monotonic() + MAX_HOLD_S
        while time.monotonic() < end:
            await asyncio.sleep(uniform(0.08, 0.25))
            px = min(max(px + uniform(-1.5, 1.5), x - 6), x + 6)
            py = min(max(py + uniform(-1.0, 1.0), y - 4), y + 4)
            await page.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": px, "y": py, "button": "left", "buttons": 1},
            )
            page.pointer = (px, py)
            now = await measure(page)
            if now is None or now[1] > spans:
                await asyncio.sleep(uniform(0.2, 0.6))
                break
    finally:
        await page.send(
            "Input.dispatchMouseEvent", {"type": "mouseReleased", "x": px, "y": py, **press}
        )
