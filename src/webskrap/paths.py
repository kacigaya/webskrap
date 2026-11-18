"""Compatibility import for :mod:`webskrap.security.paths`."""

import sys
from typing import TYPE_CHECKING

from webskrap.security import paths as _implementation

if TYPE_CHECKING:
    from webskrap.security.paths import (
        DEFAULT_OUTPUT_DIRNAME,
        MCP_PROFILE_DIR_ENV,
        OUTPUT_DIR_ENV,
        PRIVATE_DIR_MODE,
        mcp_profile_root,
        output_root,
        resolve_mcp_profile_path,
        resolve_output_path,
        secure_directory,
    )

__all__ = [
    "DEFAULT_OUTPUT_DIRNAME",
    "MCP_PROFILE_DIR_ENV",
    "OUTPUT_DIR_ENV",
    "PRIVATE_DIR_MODE",
    "mcp_profile_root",
    "output_root",
    "resolve_mcp_profile_path",
    "resolve_output_path",
    "secure_directory",
]

sys.modules[__name__] = _implementation
