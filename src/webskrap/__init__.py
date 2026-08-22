"""WebSkrap: async Playwright/Patchright scraping with coherent browser profiles.

Start with :class:`~webskrap.client.WebSkrapClient` for fetching and sessions,
:class:`~webskrap.models.SessionConfig` for how the browser is launched, and
:func:`~webskrap.profiles.get_profile` for the identity it presents. The CLI
(``webskrap``) and MCP server (``webskrap-mcp``) are built on the same objects.
"""

from webskrap.client import WebSkrapClient, WebSkrapError, WebSkrapSession
from webskrap.consent import decline_cookies
from webskrap.models import (
    BrowserProfile,
    FetchResult,
    ProxyConfig,
    ResourcePolicy,
    SessionConfig,
    Viewport,
    WebRtcIPHandlingPolicy,
)
from webskrap.profiles import get_profile, list_profiles

__all__ = [
    "BrowserProfile",
    "FetchResult",
    "ProxyConfig",
    "ResourcePolicy",
    "SessionConfig",
    "Viewport",
    "WebRtcIPHandlingPolicy",
    "WebSkrapClient",
    "WebSkrapError",
    "WebSkrapSession",
    "decline_cookies",
    "get_profile",
    "list_profiles",
]
