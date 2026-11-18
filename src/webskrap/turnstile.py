"""Compatibility import for :mod:`webskrap.challenges.turnstile`."""

import sys
from typing import TYPE_CHECKING

from webskrap.challenges import turnstile as _implementation

if TYPE_CHECKING:
    from webskrap.challenges.turnstile import (
        CLICK_INTERVAL_S,
        FRAME_HOST,
        MAX_CLICKS,
        TIMEOUT_S,
        WANDER_S,
        Checkbox,
        click,
        find_checkbox,
        locate,
        square,
        wander,
    )

__all__ = [
    "CLICK_INTERVAL_S",
    "Checkbox",
    "FRAME_HOST",
    "MAX_CLICKS",
    "TIMEOUT_S",
    "WANDER_S",
    "click",
    "find_checkbox",
    "locate",
    "square",
    "wander",
]

sys.modules[__name__] = _implementation
