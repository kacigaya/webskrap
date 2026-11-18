"""Compatibility import for :mod:`webskrap.security.urls`."""

import sys
from typing import TYPE_CHECKING

from webskrap.security import urls as _implementation

if TYPE_CHECKING:
    from webskrap.security.urls import (
        PRIVATE_NET_ENV,
        private_net_allowed,
        reject_private_network,
        validate_mcp_url,
        validate_url,
    )

__all__ = [
    "PRIVATE_NET_ENV",
    "private_net_allowed",
    "reject_private_network",
    "validate_mcp_url",
    "validate_url",
]

sys.modules[__name__] = _implementation
