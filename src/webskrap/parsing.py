"""Compatibility import for :mod:`webskrap.config.parsing`."""

import sys
from typing import TYPE_CHECKING

from webskrap.config import parsing as _implementation

if TYPE_CHECKING:
    from webskrap.config.parsing import (
        parse_element_state,
        parse_engine,
        parse_literal,
        parse_load_state,
        parse_profile_mode,
        parse_resource_policy,
        parse_session_profile_mode,
        parse_wait_until,
        parse_webrtc_ip_handling_policy,
    )

__all__ = [
    "parse_element_state",
    "parse_engine",
    "parse_literal",
    "parse_load_state",
    "parse_profile_mode",
    "parse_resource_policy",
    "parse_session_profile_mode",
    "parse_wait_until",
    "parse_webrtc_ip_handling_policy",
]

sys.modules[__name__] = _implementation
