"""Compatibility import for :mod:`webskrap.scraping.search`."""

import sys
from typing import TYPE_CHECKING

from webskrap.scraping import search as _implementation

if TYPE_CHECKING:
    from webskrap.scraping.search import (
        parse_results,
        search_url,
    )

__all__ = [
    "parse_results",
    "search_url",
]

sys.modules[__name__] = _implementation
