"""Compatibility import for :mod:`webskrap.interfaces.cli_output`."""

import sys
from typing import TYPE_CHECKING

from webskrap.interfaces import cli_output as _implementation

if TYPE_CHECKING:
    from webskrap.interfaces.cli_output import (
        OutputFormat,
        R,
        T,
        fail,
        parse_cli_value,
        parse_output_format,
        print_json,
        stderr_console,
    )

__all__ = [
    "OutputFormat",
    "R",
    "T",
    "fail",
    "parse_cli_value",
    "parse_output_format",
    "print_json",
    "stderr_console",
]

sys.modules[__name__] = _implementation
