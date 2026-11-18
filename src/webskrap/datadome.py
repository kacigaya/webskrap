"""Compatibility import for :mod:`webskrap.challenges.datadome`."""

import sys
from typing import TYPE_CHECKING

from webskrap.challenges import datadome as _implementation

if TYPE_CHECKING:
    from webskrap.challenges.datadome import (
        DRAG_STEPS,
        FRAME_HOST,
        MAX_SLIDES,
        SLIDE_INTERVAL_S,
        TIMEOUT_S,
        WANDER_S,
        Slider,
        box,
        drag_path,
        locate,
    )

__all__ = [
    "DRAG_STEPS",
    "FRAME_HOST",
    "MAX_SLIDES",
    "SLIDE_INTERVAL_S",
    "Slider",
    "TIMEOUT_S",
    "WANDER_S",
    "box",
    "drag_path",
    "locate",
]

sys.modules[__name__] = _implementation
