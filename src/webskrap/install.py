"""Compatibility import for :mod:`webskrap.browser.install`."""

import sys
from typing import TYPE_CHECKING

from webskrap.browser import install as _implementation

if TYPE_CHECKING:
    from webskrap.browser.install import (
        INDEX_URL,
        MAX_ARCHIVE_BYTES,
        browsers_dir,
        executable_in,
        extract,
        install,
        installed_executables,
        latest_download,
        platform_key,
    )

__all__ = [
    "INDEX_URL",
    "MAX_ARCHIVE_BYTES",
    "browsers_dir",
    "executable_in",
    "extract",
    "install",
    "installed_executables",
    "latest_download",
    "platform_key",
]

sys.modules[__name__] = _implementation
