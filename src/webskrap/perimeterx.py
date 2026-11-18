"""Compatibility import for :mod:`webskrap.challenges.perimeterx`."""

import sys
from typing import TYPE_CHECKING

from webskrap.challenges import perimeterx as _implementation

if TYPE_CHECKING:
    from webskrap.challenges.perimeterx import (
        CONTAINER_ID,
        HOLD_INTERVAL_S,
        MAX_HOLD_S,
        MAX_HOLDS,
        TIMEOUT_S,
        Hold,
        box,
        find_buttons,
        measure,
    )

__all__ = [
    "CONTAINER_ID",
    "HOLD_INTERVAL_S",
    "Hold",
    "MAX_HOLDS",
    "MAX_HOLD_S",
    "TIMEOUT_S",
    "box",
    "find_buttons",
    "measure",
]

sys.modules[__name__] = _implementation
