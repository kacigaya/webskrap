"""Compatibility imports for :mod:`webskrap.config.models`."""

from webskrap.config import models as _implementation
from webskrap.config.models import (
    BLOCKED_LAUNCH_ARGS,
    REMOVED_CONFIG_FIELDS,
    BrowserProfile,
    ElementState,
    GpuBackend,
    LoadState,
    ProfileMode,
    ProxyConfig,
    ResourcePolicy,
    SearchEngine,
    SessionConfig,
    SessionProfileMode,
    Viewport,
    WaitUntil,
    WebRtcIPHandlingPolicy,
    native_profile_env,
    user_agent_metadata,
)
from webskrap.driver.text_search import (
    TextWindow,
    text_window,
)
from webskrap.scraping.markup import (
    Element,
    parse_html,
)
from webskrap.scraping.results import (
    SHAPED_RESPONSE_HEADERS,
    FetchResult,
    Link,
    SearchHit,
    SearchResult,
    shape_fetch_result,
    shape_search_result,
)

__all__ = [
    "BLOCKED_LAUNCH_ARGS",
    "BrowserProfile",
    "Element",
    "ElementState",
    "FetchResult",
    "GpuBackend",
    "Link",
    "LoadState",
    "ProfileMode",
    "ProxyConfig",
    "REMOVED_CONFIG_FIELDS",
    "ResourcePolicy",
    "SHAPED_RESPONSE_HEADERS",
    "SearchEngine",
    "SearchHit",
    "SearchResult",
    "SessionConfig",
    "SessionProfileMode",
    "TextWindow",
    "Viewport",
    "WaitUntil",
    "WebRtcIPHandlingPolicy",
    "native_profile_env",
    "parse_html",
    "shape_fetch_result",
    "shape_search_result",
    "text_window",
    "user_agent_metadata",
]


def __getattr__(name: str) -> object:
    """Resolve legacy private attributes from the original implementation."""
    return getattr(_implementation, name)
