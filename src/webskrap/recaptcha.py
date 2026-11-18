"""Compatibility import for :mod:`webskrap.challenges.recaptcha`."""

import sys
from typing import TYPE_CHECKING

from webskrap.challenges import recaptcha as _implementation

if TYPE_CHECKING:
    from webskrap.challenges.recaptcha import (
        CHECK_TIMEOUT_S,
        CLICK_INTERVAL_S,
        DRAW_WAIT_S,
        MAX_CLICKS,
        TIMEOUT_S,
        WANDER_S,
        Tick,
        Widget,
        box,
        locate,
        provider_of,
        solve,
    )

__all__ = [
    "CHECK_TIMEOUT_S",
    "CLICK_INTERVAL_S",
    "DRAW_WAIT_S",
    "MAX_CLICKS",
    "TIMEOUT_S",
    "Tick",
    "WANDER_S",
    "Widget",
    "box",
    "locate",
    "provider_of",
    "solve",
]

sys.modules[__name__] = _implementation
