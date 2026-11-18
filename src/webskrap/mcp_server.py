"""Compatibility import for :mod:`webskrap.interfaces.mcp_server`."""

import sys
from typing import TYPE_CHECKING

from webskrap.interfaces import mcp_server as _implementation

if TYPE_CHECKING:
    from webskrap.interfaces.mcp_server import (
        EVAL_ENV,
        INSTRUCTIONS,
        MAX_EVAL_CHARS,
        T,
        browser_close,
        browser_eval,
        browser_find,
        browser_goto,
        browser_insert_text,
        browser_interact,
        browser_list,
        browser_mouse,
        browser_open,
        browser_press,
        browser_screenshot,
        browser_snapshot,
        browser_solve_captcha,
        browser_tabs,
        browser_text,
        browser_view,
        browser_wait_for,
        doctor,
        fetch,
        fetch_session_close,
        guide_resource,
        logger,
        main,
        mcp,
        profiles_resource,
        search,
        sessions_resource,
        stealth_fetch,
    )

__all__ = [
    "EVAL_ENV",
    "INSTRUCTIONS",
    "MAX_EVAL_CHARS",
    "T",
    "browser_close",
    "browser_eval",
    "browser_find",
    "browser_goto",
    "browser_insert_text",
    "browser_interact",
    "browser_list",
    "browser_mouse",
    "browser_open",
    "browser_press",
    "browser_screenshot",
    "browser_snapshot",
    "browser_solve_captcha",
    "browser_tabs",
    "browser_text",
    "browser_view",
    "browser_wait_for",
    "doctor",
    "fetch",
    "fetch_session_close",
    "guide_resource",
    "logger",
    "main",
    "mcp",
    "profiles_resource",
    "search",
    "sessions_resource",
    "stealth_fetch",
]

if __name__ == "__main__":
    _implementation.main()

sys.modules[__name__] = _implementation
