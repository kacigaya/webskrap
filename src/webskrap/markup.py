"""Compatibility import for :mod:`webskrap.scraping.markup`."""

import sys
from typing import TYPE_CHECKING

from webskrap.scraping import markup as _implementation

if TYPE_CHECKING:
    from webskrap.scraping.markup import (
        Element,
        parse_html,
    )

__all__ = [
    "Element",
    "parse_html",
]

sys.modules[__name__] = _implementation
