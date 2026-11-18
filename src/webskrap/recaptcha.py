"""reCAPTCHA v2 and hCaptcha checkbox: find it and click it the way a person would.

Opt-in through ``solve_recaptcha`` for a page that blocks the site, where
:func:`webskrap.challenges.settle` drives :class:`Tick`, and through
:func:`solve` for a checkbox inside a form (the ``browser_solve_captcha``
action).

Both widgets draw the checkbox in a cross-site iframe: reCAPTCHA's ``anchor``
frame from ``www.google.com`` or ``www.recaptcha.net``, and hCaptcha's frame
from ``*.hcaptcha.com``. Both mark it ``role=checkbox`` and report the result
in ``aria-checked``. CDP's DOM reads them without running anything in the
page, and the box is measured again right before every click.

The click is Turnstile's (:func:`webskrap.turnstile.click`). When the provider
trusts the browser, the box ticks and the page's own callback continues. When
it does not, it opens an image challenge in a second frame of the same
provider, parked off-screen until then. That challenge is never touched: the
solver stops and the call reports ``blocked`` or ``challenge``.

reCAPTCHA v3 and invisible widgets have no checkbox and are not handled. No
token is read, copied or injected, and no outside solving service is used.
"""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from webskrap import cdp_dom
from webskrap.cdp import CDPPage
from webskrap.errors import WebSkrapError
from webskrap.human import Box, uniform
from webskrap.turnstile import click, wander

TIMEOUT_S = 60.0
CLICK_INTERVAL_S = 15.0
MAX_CLICKS = 2
CHECK_TIMEOUT_S = 15.0
DRAW_WAIT_S = 5.0
WANDER_S = (1.0, 2.5)
_MIN_SIDE = 4.0
_OFF_SCREEN = -1000.0
_RECAPTCHA_HOSTS = ("www.google.com", "www.recaptcha.net")
_RECAPTCHA_PATH = re.compile(r"^/recaptcha/(api2|enterprise)/(anchor|bframe)$")


@dataclass
class Widget:
    """One checkbox widget: its provider, checkbox box and state."""

    provider: str
    box: Box | None
    checked: bool
    challenged: bool


@dataclass
class Tick:
    """Clicks on one checkbox that blocks the page.

    :meth:`step` is polled until the page clears. ``seen`` turns True once the
    widget has been drawn. An image challenge ends the attempt: the deadline
    moves to now, so the caller stops waiting.
    """

    page: CDPPage
    deadline: float = field(default_factory=lambda: time.monotonic() + TIMEOUT_S)
    clicks: int = 0
    next_click: float = 0.0
    seen: bool = False

    @property
    def attempts(self) -> int:
        """How many gestures have been made so far."""
        return self.clicks

    async def step(self) -> None:
        """Click the checkbox when it is unticked and a click is due; else do nothing."""
        now = time.monotonic()
        if self.clicks >= MAX_CLICKS or now < self.next_click or now >= self.deadline:
            return
        widget = await locate(self.page)
        if widget is None:
            return
        self.seen = True
        if widget.challenged:
            self.deadline = now
            return
        if widget.checked or widget.box is None:
            return
        with suppress(WebSkrapError):
            if self.clicks == 0:
                await wander(self.page, uniform(*WANDER_S))
                widget = await locate(self.page)
                if widget is None or widget.box is None or widget.checked or widget.challenged:
                    return
            await click(self.page, widget.box)
        self.clicks += 1
        self.next_click = time.monotonic() + CLICK_INTERVAL_S


async def solve(page: CDPPage, timeout_s: float = CHECK_TIMEOUT_S) -> dict[str, Any]:
    """Click the checkbox on ``page`` once and report what happened.

    Returns ``{"provider", "state"}``. ``state`` is ``checked`` (ticked),
    ``challenge`` (an image challenge opened; it is left alone), ``gone`` (the
    widget left the page, usually because its callback navigated),
    ``timeout`` (neither within ``timeout_s``), or ``not_found`` (no drawn
    widget within :data:`DRAW_WAIT_S`, since its script renders it late).
    """
    drawn_by = time.monotonic() + DRAW_WAIT_S
    widget = await locate(page)
    while (widget is None or (widget.box is None and not widget.challenged)) and (
        time.monotonic() < drawn_by
    ):
        await asyncio.sleep(0.25)
        widget = await locate(page)
    if widget is None:
        return {"provider": None, "state": "not_found"}
    provider = widget.provider
    if widget.challenged:
        return {"provider": provider, "state": "challenge"}
    if widget.checked:
        return {"provider": provider, "state": "checked"}
    if widget.box is None:
        return {"provider": provider, "state": "not_found"}
    await click(page, widget.box)
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        await asyncio.sleep(0.25)
        widget = await locate(page)
        if widget is None:
            return {"provider": provider, "state": "gone"}
        if widget.challenged:
            return {"provider": provider, "state": "challenge"}
        if widget.checked:
            return {"provider": provider, "state": "checked"}
    return {"provider": provider, "state": "timeout"}


def provider_of(url: str) -> str | None:
    """Return the provider whose widget frame ``url`` is, or None."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host in _RECAPTCHA_HOSTS and _RECAPTCHA_PATH.match(parts.path):
        return "reCAPTCHA"
    if (host == "hcaptcha.com" or host.endswith(".hcaptcha.com")) and parts.path.startswith(
        "/captcha/"
    ):
        return "hCaptcha"
    return None


async def locate(page: CDPPage) -> Widget | None:
    """Return the first checkbox widget on ``page``, or None when there is none.

    A provider's challenge frame counts as open once it is on screen.
    """
    try:
        frames = await cdp_dom.frames(page)
    except WebSkrapError:
        return None
    widget: Widget | None = None
    challenged = False
    for frame in frames:
        if frame.owner is None or frame.page.session_id == page.session_id:
            continue
        provider = provider_of(frame.url)
        if provider is None:
            continue
        widget = widget or Widget(provider, None, False, False)
        if await _is_challenge(page, frame):
            challenged = challenged or (
                frame.offset_x > _OFF_SCREEN and frame.offset_y > _OFF_SCREEN
            )
            continue
        found = await _checkbox_in(frame.page)
        if found is not None and widget.box is None:
            box, checked = found
            moved: Box = {**box, "x": box["x"] + frame.offset_x, "y": box["y"] + frame.offset_y}
            widget = Widget(provider, moved, checked, False)
    if widget is not None:
        widget.challenged = challenged
    return widget


async def _is_challenge(page: CDPPage, frame: cdp_dom.Frame) -> bool:
    """Tell a provider's image challenge frame from its checkbox frame.

    reCAPTCHA names it in the path (``bframe``). hCaptcha serves both from one
    page and names them only in the ``<iframe>`` element's ``#frame=``
    fragment, which the frame's own URL leaves out.
    """
    if urlsplit(frame.url).path.endswith("/bframe"):
        return True
    if frame.owner is None or frame.owner[0] != page.session_id:
        return False
    try:
        node = await page.send("DOM.describeNode", {"backendNodeId": frame.owner[1]})
    except WebSkrapError:
        return False
    attributes = node["node"].get("attributes", [])
    src = dict(zip(attributes[::2], attributes[1::2], strict=False)).get("src", "")
    return "frame=challenge" in urlsplit(src).fragment


async def _checkbox_in(frame: CDPPage) -> tuple[Box, bool] | None:
    """Return the drawn checkbox in ``frame``'s viewport and whether it is ticked."""
    try:
        document = await frame.send("DOM.getDocument", {"depth": 0})
        found = await frame.send(
            "DOM.querySelector",
            {"nodeId": document["root"]["nodeId"], "selector": "[role=checkbox]"},
        )
        node_id = found.get("nodeId")
        if not node_id:
            return None
        model = await frame.send("DOM.getBoxModel", {"nodeId": node_id})
        attributes = (await frame.send("DOM.getAttributes", {"nodeId": node_id}))["attributes"]
    except WebSkrapError:
        return None
    drawn = box(model["model"]["content"])
    if drawn is None:
        return None
    names = dict(zip(attributes[::2], attributes[1::2], strict=False))
    return drawn, names.get("aria-checked") == "true"


def box(quad: list[float]) -> Box | None:
    """Return a content quad as a box, or None when it is too small to click."""
    width, height = quad[2] - quad[0], quad[5] - quad[1]
    if width < _MIN_SIDE or height < _MIN_SIDE:
        return None
    return {"x": quad[0], "y": quad[1], "width": width, "height": height}
