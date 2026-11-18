"""Compatibility imports for :mod:`webskrap.driver.cdp`."""

from webskrap.browser.executable import _SYSTEM_CHROMIUM as _SYSTEM_CHROMIUM
from webskrap.browser.executable import _channel_paths as _channel_paths
from webskrap.browser.executable import _playwright_browsers_dirs as _playwright_browsers_dirs
from webskrap.browser.executable import _revision as _revision
from webskrap.browser.executable import find_executable
from webskrap.driver import cdp as _implementation
from webskrap.driver.cdp import (
    CLOSE_TIMEOUT_S,
    LIFECYCLE_EVENTS,
    MAX_MESSAGE_BYTES,
    CDPBrowser,
    CDPConnection,
    CDPError,
    CDPPage,
    EventHandler,
    EventRecorder,
    PipeTransport,
    Transport,
    WebSocketTransport,
    base_args,
    launch,
    logger,
)

__all__ = [
    "CDPBrowser",
    "CDPConnection",
    "CDPError",
    "CDPPage",
    "CLOSE_TIMEOUT_S",
    "EventHandler",
    "EventRecorder",
    "LIFECYCLE_EVENTS",
    "MAX_MESSAGE_BYTES",
    "PipeTransport",
    "Transport",
    "WebSocketTransport",
    "base_args",
    "find_executable",
    "launch",
    "logger",
]


def __getattr__(name: str) -> object:
    """Resolve legacy private attributes from the original implementation."""
    return getattr(_implementation, name)
