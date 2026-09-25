"""Human-shaped input: cursor paths, clicks, typing and scrolling.

Playwright's own input is easy to tell from a person's: ``click`` teleports
the cursor and presses the button for a couple of milliseconds, ``fill`` sets a
value with no key events at all, and ``scroll_into_view_if_needed`` scrolls
without a single wheel event. Behavioral detectors score exactly those
signals. The helpers here drive the real mouse and keyboard instead, with
timings drawn from human ranges, and take a locator so they work inside
frames and on both the Playwright and the Patchright driver.
"""

from __future__ import annotations

from collections.abc import Mapping
from secrets import SystemRandom
from typing import Any

from patchright.async_api import Locator as PatchrightLocator
from patchright.async_api import Page as PatchrightPage
from playwright.async_api import FloatRect
from playwright.async_api import Locator as PlaywrightLocator
from playwright.async_api import Page as PlaywrightPage

from webskrap.errors import ErrorCode, WebSkrapError

#: A page from either driver; both expose the same input API.
AnyPage = PlaywrightPage | PatchrightPage
#: A locator from either driver.
AnyLocator = PlaywrightLocator | PatchrightLocator

# Cursor jitter below is pixel offsets and sleep durations, never a token,
# identifier, or security decision, so `random` would be adequate. It draws
# from system entropy anyway: a few dozen values per click cost nothing next to
# the millisecond sleeps between mouse moves, and it keeps the module free of
# predictable-RNG calls that a security scanner would have to be told to ignore.
uniform = SystemRandom().uniform


async def click(
    page: AnyPage,
    locator: AnyLocator,
    *,
    description: str,
    **click_options: Any,
) -> None:
    """Click ``locator`` along a curved, variable-speed cursor path.

    ``page`` owns the mouse; ``locator`` may live in any of its frames, since
    bounding boxes are reported in the top-level viewport's coordinates.
    ``description`` names the target in errors.

    Args:
        page: Page whose mouse and keyboard drive the click.
        locator: Element to click.
        description: Human-readable target, used in error messages.
        **click_options: ``position``, ``timeout``, ``strict``, ``trial``,
            ``modifiers``, ``button``, ``click_count`` and ``delay``.

    Raises:
        WebSkrapError: If ``strict`` was requested and the locator matches
            more than one element, or the element has no visible bounding box.
    """
    timeout = click_options.get("timeout")
    await locator.wait_for(state="visible", timeout=timeout)
    await locator.scroll_into_view_if_needed(timeout=timeout)

    if click_options.get("strict") is True and await locator.count() != 1:
        msg = f"strict mode expected one element for selector: {description}"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)

    box = await locator.bounding_box(timeout=timeout)
    if box is None:
        msg = f"could not find a visible bounding box for selector: {description}"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)

    x, y = click_point(box, click_options.get("position"))
    if click_options.get("trial"):
        return

    await page.wait_for_timeout(uniform(80, 220))

    start_x = x + uniform(-160, 160)
    start_y = y + uniform(-90, 90)
    await page.mouse.move(start_x, start_y, steps=1)
    end_x = x + uniform(-8, 8)
    end_y = y + uniform(-6, 6)
    distance = ((end_x - start_x) ** 2 + (end_y - start_y) ** 2) ** 0.5
    steps = max(12, min(48, int(distance / 6)))
    for px, py in bezier_path((start_x, start_y), (end_x, end_y), steps):
        await page.mouse.move(px, py, steps=1)
        await page.wait_for_timeout(uniform(2, 9))
    await page.wait_for_timeout(uniform(40, 140))

    mouse_options = mouse_click_options(click_options)
    modifiers = click_options.get("modifiers") or []
    for modifier in modifiers:
        await page.keyboard.down(modifier)
    try:
        await page.mouse.click(x, y, **mouse_options)
    finally:
        for modifier in reversed(modifiers):
            await page.keyboard.up(modifier)


def click_point(
    box: FloatRect,
    position: Mapping[str, float] | None,
) -> tuple[float, float]:
    """Return where to click in ``box``: ``position`` if given, else near its center."""
    if position is not None:
        return box["x"] + position["x"], box["y"] + position["y"]

    jitter_x = min(box["width"] * 0.2, 6)
    jitter_y = min(box["height"] * 0.2, 6)
    return (
        box["x"] + box["width"] / 2 + uniform(-jitter_x, jitter_x),
        box["y"] + box["height"] / 2 + uniform(-jitter_y, jitter_y),
    )


def bezier_path(
    start: tuple[float, float],
    end: tuple[float, float],
    steps: int,
) -> list[tuple[float, float]]:
    """Curved, eased cursor path from start to end.

    Mimics HumanCursor's trajectory: a cubic Bezier bent off the straight line
    by randomized control points, with smoothstep-eased spacing so velocity
    ramps up then slows near the target instead of moving in a straight,
    evenly-spaced line (the linear ``mouse.move(steps=n)`` robot tell).
    """
    x0, y0 = start
    x3, y3 = end
    dx, dy = x3 - x0, y3 - y0
    distance = max(1.0, (dx * dx + dy * dy) ** 0.5)
    nx, ny = -dy / distance, dx / distance  # unit normal to the straight line
    bend = distance * uniform(0.08, 0.22) * (1 if uniform(0, 1) < 0.5 else -1)
    cx1 = x0 + dx / 3 + nx * bend * uniform(0.6, 1.0)
    cy1 = y0 + dy / 3 + ny * bend * uniform(0.6, 1.0)
    cx2 = x0 + dx * 2 / 3 + nx * bend * uniform(0.6, 1.0)
    cy2 = y0 + dy * 2 / 3 + ny * bend * uniform(0.6, 1.0)

    points: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)  # smoothstep -> non-uniform speed
        mt = 1 - t
        bx = mt**3 * x0 + 3 * mt**2 * t * cx1 + 3 * mt * t**2 * cx2 + t**3 * x3
        by = mt**3 * y0 + 3 * mt**2 * t * cy1 + 3 * mt * t**2 * cy2 + t**3 * y3
        taper = mt  # jitter fades to zero at the target
        points.append((bx + uniform(-1.2, 1.2) * taper, by + uniform(-1.2, 1.2) * taper))
    points[-1] = (x3, y3)
    return points


def mouse_click_options(click_options: dict[str, Any]) -> dict[str, Any]:
    """Return the subset of ``click_options`` that ``page.mouse.click`` accepts."""
    mouse_options: dict[str, Any] = {}
    if "button" in click_options:
        mouse_options["button"] = click_options["button"]
    if "click_count" in click_options:
        mouse_options["click_count"] = click_options["click_count"]
    if "delay" in click_options:
        mouse_options["delay"] = click_options["delay"]
    return mouse_options
