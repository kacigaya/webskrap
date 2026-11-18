"""Compatibility import for :mod:`webskrap.config.profiles`."""

import sys
from typing import TYPE_CHECKING

from webskrap.config import profiles as _implementation

if TYPE_CHECKING:
    from webskrap.config.profiles import (
        get_profile,
        list_profiles,
    )

__all__ = [
    "get_profile",
    "list_profiles",
]

sys.modules[__name__] = _implementation
