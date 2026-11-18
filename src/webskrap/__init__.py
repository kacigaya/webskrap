"""WebSkrap: async Chromium scraping over its own CDP client, with coherent profiles.

Start with :class:`~webskrap.client.WebSkrapClient` for fetching and sessions,
:class:`~webskrap.models.SessionConfig` for how the browser is launched, and
:func:`~webskrap.profiles.get_profile` for the identity it presents. The CLI
(``webskrap``) and MCP server (``webskrap-mcp``) are built on the same objects.
"""

from webskrap.browser.consent import decline_cookies
from webskrap.config.models import (
    BrowserProfile,
    GpuBackend,
    ProxyConfig,
    ResourcePolicy,
    SearchEngine,
    SessionConfig,
    Viewport,
    WebRtcIPHandlingPolicy,
)
from webskrap.config.profiles import get_profile, list_profiles
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.scraping.client import WebSkrapClient, WebSkrapSession
from webskrap.scraping.markup import Element, parse_html
from webskrap.scraping.results import FetchResult, Link, SearchHit, SearchResult

__all__ = [
    "BrowserProfile",
    "Element",
    "ErrorCode",
    "FetchResult",
    "Link",
    "ProxyConfig",
    "GpuBackend",
    "ResourcePolicy",
    "SearchEngine",
    "SearchHit",
    "SearchResult",
    "SessionConfig",
    "Viewport",
    "WebRtcIPHandlingPolicy",
    "WebSkrapClient",
    "WebSkrapError",
    "WebSkrapSession",
    "decline_cookies",
    "get_profile",
    "list_profiles",
    "parse_html",
]
