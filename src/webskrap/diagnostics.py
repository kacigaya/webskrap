"""Compatibility import for :mod:`webskrap.browser.diagnostics`."""

import sys
from typing import TYPE_CHECKING

from webskrap.browser import diagnostics as _implementation

if TYPE_CHECKING:
    from webskrap.browser.diagnostics import (
        ENVIRONMENT_VARIABLES,
        UTC_ZONES,
        diagnose,
        fingerprint_warnings,
        font_count,
        host_timezone,
        package_version,
    )

__all__ = [
    "ENVIRONMENT_VARIABLES",
    "UTC_ZONES",
    "diagnose",
    "fingerprint_warnings",
    "font_count",
    "host_timezone",
    "package_version",
]

sys.modules[__name__] = _implementation
