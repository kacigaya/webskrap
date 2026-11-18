"""Compatibility import for :mod:`webskrap.scraping.fetch_runtime`."""

import sys
from typing import TYPE_CHECKING

from webskrap.scraping import fetch_runtime as _implementation

if TYPE_CHECKING:
    from webskrap.scraping.fetch_runtime import (
        MAX_FETCH_CONCURRENCY,
        MAX_FETCH_SESSIONS,
        FetchRuntime,
    )

__all__ = [
    "FetchRuntime",
    "MAX_FETCH_CONCURRENCY",
    "MAX_FETCH_SESSIONS",
]

sys.modules[__name__] = _implementation
