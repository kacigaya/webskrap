"""Human-shaped input timing: cursor paths and click points.

Mechanical input is easy to tell from a person's: a cursor that teleports, a
button held for two milliseconds, a page that scrolls without a wheel event.
Behavioral detectors score exactly those signals. The helpers here produce
the human-range values; :mod:`webskrap.cdp_dom` sends them as trusted
``Input.dispatch*`` events.
"""

from __future__ import annotations

from collections.abc import Mapping
from secrets import SystemRandom
from typing import TypedDict

# Cursor jitter below is pixel offsets and sleep durations, never a token,
# identifier, or security decision, so `random` would be adequate. It draws
# from system entropy anyway: a few dozen values per click cost nothing next to
# the millisecond sleeps between mouse moves, and it keeps the module free of
# predictable-RNG calls that a security scanner would have to be told to ignore.
uniform = SystemRandom().uniform


class Box(TypedDict):
    """An element's box in CSS pixels."""

    x: float
    y: float
    width: float
    height: float


#: Viewport margin, in CSS pixels, an element must clear to count as in view.
VIEW_MARGIN = 40
#: Wheel batches before giving up and jumping with scrollIntoView.
MAX_WHEEL_BATCHES = 12


def click_point(
    box: Box,
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
