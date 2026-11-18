"""Compatibility import for :mod:`webskrap.driver.cdp_session`."""

from webskrap.browser.navigation import DECLINE_TIMEOUT_MS as DECLINE_TIMEOUT_MS
from webskrap.browser.navigation import goto as goto
from webskrap.driver import cdp_session as _implementation
from webskrap.driver.cdp_session import (
    CLOSE_TAB_TIMEOUT_MS,
    PAGE_ORDER_FILE,
    POINTER_FILE,
    TAB_SELECTION_FILE,
    T,
    attach,
    body_text,
    close_tab,
    connect,
    current_page,
    devtools_path,
    evaluate,
    history,
    insert_text,
    key_events,
    list_tabs,
    mouse_action,
    new_tab,
    page_state,
    poll,
    press,
    read_text,
    save_pointer,
    saved_pointer,
    screenshot,
    select_tab,
    view,
    viewport,
    wait_for,
)

__all__ = [
    "CLOSE_TAB_TIMEOUT_MS",
    "DECLINE_TIMEOUT_MS",
    "PAGE_ORDER_FILE",
    "POINTER_FILE",
    "T",
    "TAB_SELECTION_FILE",
    "attach",
    "body_text",
    "close_tab",
    "connect",
    "current_page",
    "devtools_path",
    "evaluate",
    "goto",
    "history",
    "insert_text",
    "key_events",
    "list_tabs",
    "mouse_action",
    "new_tab",
    "page_state",
    "poll",
    "press",
    "read_text",
    "save_pointer",
    "saved_pointer",
    "screenshot",
    "select_tab",
    "view",
    "viewport",
    "wait_for",
]


def __getattr__(name: str) -> object:
    """Resolve legacy private attributes from the original implementation."""
    return getattr(_implementation, name)
