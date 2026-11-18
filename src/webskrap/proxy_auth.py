"""Compatibility import for :mod:`webskrap.browser.proxy_auth`."""

import sys
from typing import TYPE_CHECKING

from webskrap.browser import proxy_auth as _implementation

if TYPE_CHECKING:
    from webskrap.browser.proxy_auth import (
        READY,
        main,
        serve,
    )

__all__ = [
    "READY",
    "main",
    "serve",
]

if __name__ == "__main__":
    raise SystemExit(_implementation.main(sys.argv[1:]))

sys.modules[__name__] = _implementation
