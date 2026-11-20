"""WebSkrap: async Chromium scraping over its own CDP client, with coherent profiles.

Start with :class:`~webskrap.scraping.client.WebSkrapClient` for fetching and sessions,
:class:`~webskrap.config.models.SessionConfig` for how the browser is launched, and
:func:`~webskrap.config.profiles.get_profile` for the identity it presents. The CLI
(``webskrap``) and MCP server (``webskrap-mcp``) are built on the same objects.
"""

from webskrap.browser.consent import decline_cookies
from webskrap.config.models import (
    GpuBackend,
    ResourcePolicy,
    SessionConfig,
    WebRtcIPHandlingPolicy,
)
from webskrap.config.profile import BrowserProfile, ProxyConfig, Viewport
from webskrap.config.profiles import get_profile, list_profiles
from webskrap.config.search import SearchEngine
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.scraping.client import WebSkrapClient
from webskrap.scraping.markup import Element, parse_html
from webskrap.scraping.results import FetchResult, Link, SearchHit, SearchResult
from webskrap.scraping.session import WebSkrapSession

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
