"""Compatibility imports for :mod:`webskrap.driver.cdp`."""

from webskrap.browser.executable import _SYSTEM_CHROMIUM as _SYSTEM_CHROMIUM
from webskrap.browser.executable import _channel_paths as _channel_paths
from webskrap.browser.executable import _playwright_browsers_dirs as _playwright_browsers_dirs
from webskrap.browser.executable import _revision as _revision
from webskrap.browser.executable import find_executable
from webskrap.driver import cdp as _implementation
from webskrap.driver.cdp import (
    LIFECYCLE_EVENTS,
    CDPConnection,
    CDPError,
    CDPPage,
    EventHandler,
    EventRecorder,
    Transport,
    logger,
)
from webskrap.driver.launch import _LOG_TAIL_BYTES as _LOG_TAIL_BYTES
from webskrap.driver.launch import _POSIX_ONLY as _POSIX_ONLY
from webskrap.driver.launch import CLOSE_TIMEOUT_S, CDPBrowser, base_args, launch
from webskrap.driver.launch import _exit_error as _exit_error
from webskrap.driver.launch import _high_fd as _high_fd
from webskrap.driver.launch import _launch_on_pipe as _launch_on_pipe
from webskrap.driver.launch import _launch_on_port as _launch_on_port
from webskrap.driver.launch import _log_tail as _log_tail
from webskrap.driver.launch import _reap as _reap
from webskrap.driver.launch import _stop_process as _stop_process
from webskrap.driver.transport import _WEBSOCKET_GUID as _WEBSOCKET_GUID
from webskrap.driver.transport import MAX_MESSAGE_BYTES, PipeTransport, WebSocketTransport
from webskrap.driver.transport import _mask as _mask

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
