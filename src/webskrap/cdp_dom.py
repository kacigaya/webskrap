"""Compatibility import for :mod:`webskrap.driver.cdp_dom`."""

from webskrap.driver import cdp_dom as _implementation
from webskrap.driver.cdp_dom import (
    MOUSE_PRESSED_FORCE,
    REF_PATTERN,
    Frame,
    element_action,
    element_clip,
    element_state,
    frame_key,
    frames,
    human_click,
    instant_click,
    is_visible,
    query_elements,
    start_auto_attach,
    travel,
)
from webskrap.driver.snapshot import _SNAPSHOT_SCRIPT as _SNAPSHOT_SCRIPT
from webskrap.driver.snapshot import _frame_snapshot as _frame_snapshot
from webskrap.driver.snapshot import matching_snapshot as matching_snapshot
from webskrap.driver.snapshot import snapshot as snapshot

__all__ = [
    "Frame",
    "MOUSE_PRESSED_FORCE",
    "REF_PATTERN",
    "element_action",
    "element_clip",
    "element_state",
    "frame_key",
    "frames",
    "human_click",
    "instant_click",
    "is_visible",
    "matching_snapshot",
    "query_elements",
    "snapshot",
    "start_auto_attach",
    "travel",
]


def __getattr__(name: str) -> object:
    """Resolve legacy private attributes from the original implementation."""
    return getattr(_implementation, name)
