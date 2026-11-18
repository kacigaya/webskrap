"""Compatibility import for :mod:`webskrap.browser.display`."""

import sys
from typing import TYPE_CHECKING

from webskrap.browser import display as _implementation

if TYPE_CHECKING:
    from webskrap.browser.display import (
        DIRECTORY_PREFIX,
        START_TIMEOUT_S,
        STOP_TIMEOUT_S,
        VirtualDisplay,
        stop_detached,
        write_cookie_file,
    )

__all__ = [
    "DIRECTORY_PREFIX",
    "START_TIMEOUT_S",
    "STOP_TIMEOUT_S",
    "VirtualDisplay",
    "stop_detached",
    "write_cookie_file",
]

sys.modules[__name__] = _implementation
