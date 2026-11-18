"""Compatibility import for :mod:`webskrap.driver.text_search`."""

import sys
from typing import TYPE_CHECKING

from webskrap.driver import text_search as _implementation

if TYPE_CHECKING:
    from webskrap.driver.text_search import (
        MAX_CONTEXT_LINES,
        MAX_LINE_CHARS,
        MAX_MATCHES,
        MAX_PATTERN_CHARS,
        PATTERN_TIMEOUT_S,
        search_lines,
        validate_search,
    )

__all__ = [
    "MAX_CONTEXT_LINES",
    "MAX_LINE_CHARS",
    "MAX_MATCHES",
    "MAX_PATTERN_CHARS",
    "PATTERN_TIMEOUT_S",
    "search_lines",
    "validate_search",
]

sys.modules[__name__] = _implementation
