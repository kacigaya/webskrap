"""Compatibility import for :mod:`webskrap.interfaces.browser_cli`."""

import sys
from typing import TYPE_CHECKING

from webskrap.interfaces import browser_cli as _implementation

if TYPE_CHECKING:
    from webskrap.interfaces.browser_cli import (
        ActionTimeoutOption,
        FormatOption,
        GpuOption,
        ProfileOption,
        SessionOption,
        SessionProfileModeOption,
        T,
        VirtualDisplayOption,
        browser_app,
        close_command,
        console,
        eval_command,
        goto_command,
        insert_text_command,
        list_command,
        mouse_command,
        open_command,
        press_command,
        screenshot_command,
        snapshot_command,
        solve_captcha_command,
        tab_close_command,
        tab_list_command,
        tab_new_command,
        tab_select_command,
        view_command,
        wait_command,
    )

__all__ = [
    "ActionTimeoutOption",
    "FormatOption",
    "GpuOption",
    "ProfileOption",
    "SessionOption",
    "SessionProfileModeOption",
    "T",
    "VirtualDisplayOption",
    "browser_app",
    "close_command",
    "console",
    "eval_command",
    "goto_command",
    "insert_text_command",
    "list_command",
    "mouse_command",
    "open_command",
    "press_command",
    "screenshot_command",
    "snapshot_command",
    "solve_captcha_command",
    "tab_close_command",
    "tab_list_command",
    "tab_new_command",
    "tab_select_command",
    "view_command",
    "wait_command",
]

sys.modules[__name__] = _implementation
