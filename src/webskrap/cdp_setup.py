"""Compatibility import for :mod:`webskrap.driver.cdp_setup`."""

import sys
from typing import TYPE_CHECKING

from webskrap.driver import cdp_setup as _implementation

if TYPE_CHECKING:
    from webskrap.driver.cdp_setup import (
        BLOCKED_TYPES,
        apply_storage_state,
        intercept,
        load_storage_state,
        prepare_browser,
        prepare_page,
        served_page,
    )

__all__ = [
    "BLOCKED_TYPES",
    "apply_storage_state",
    "intercept",
    "load_storage_state",
    "prepare_browser",
    "prepare_page",
    "served_page",
]

sys.modules[__name__] = _implementation
