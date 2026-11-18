"""Compatibility import for :mod:`webskrap.driver.human`."""

import sys
from typing import TYPE_CHECKING

from webskrap.driver import human as _implementation

if TYPE_CHECKING:
    from webskrap.driver.human import (
        MAX_WHEEL_BATCHES,
        VIEW_MARGIN,
        Box,
        bezier_path,
        click_point,
        uniform,
    )

__all__ = [
    "Box",
    "MAX_WHEEL_BATCHES",
    "VIEW_MARGIN",
    "bezier_path",
    "click_point",
    "uniform",
]

sys.modules[__name__] = _implementation
