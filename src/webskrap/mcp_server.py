"""Model Context Protocol server exposing WebSkrap over stdio.

Run with ``webskrap-mcp`` (after ``pip install webskrap``) or
``python -m webskrap.mcp_server``. Point an MCP client (Claude Desktop, Claude
Code, ...) at that command to drive scraping through the tools below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from webskrap.client import WebSkrapClient, WebSkrapError, browser_doctor
from webskrap.models import SessionConfig, shape_fetch_result
from webskrap.parsing import (
    parse_resource_policy,
    parse_wait_until,
    parse_webrtc_ip_handling_policy,
)
from webskrap.profiles import get_profile

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - optional dependency
    msg = "the MCP server requires mcp. Run: pip install webskrap"
    raise WebSkrapError(msg) from exc

mcp = FastMCP("webskrap")


@mcp.tool()
async def fetch(
    url: str,
    profile: str = "desktop-chrome",
    channel: str = "chrome",
    wait_until: str = "networkidle",
    resource_policy: str = "all",
    timeout_ms: float = 60_000,
    max_chars: int = 20_000,
    text_only: bool = True,
    decline_cookies: bool = True,
) -> dict[str, Any]:
    """Fetch a URL with the Patchright stealth driver and return page data.

    Uses the same CDP-leak-free headless-Chrome stealth path as the CLI, so
    JS-heavy and anti-bot pages that block naive scrapers still load. Waits for
    networkidle by default so single-page apps have hydrated before reading.
    Returns clean visible page text by default (LLM-friendly, no HTML tags); set
    text_only=False to get the raw HTML instead. For finer stealth control
    (fingerprint surface, WebRTC, UA masking, persistent profile) use
    stealth_fetch.

    Args:
        url: The URL to load.
        profile: Profile label; native Patchright defaults remain authoritative.
        channel: Browser channel, e.g. chrome. Use chromium on Linux ARM64.
        wait_until: commit, domcontentloaded, load, or networkidle.
        resource_policy: all, lite (block images/fonts/media), or documents.
        timeout_ms: Navigation timeout in milliseconds.
        max_chars: Maximum characters of page text to return.
        text_only: Return clean visible text (default) instead of raw HTML.
        decline_cookies: Click a cookie consent notice's reject button after
            load, so the banner does not bury the page text.
    """
    config = SessionConfig(
        driver="patchright",
        channel=channel,
        headless=True,
        navigation_timeout_ms=timeout_ms,
        resource_policy=parse_resource_policy(resource_policy),
        decline_cookies=decline_cookies,
    )
    async with WebSkrapClient() as client:
        result = await client.fetch(
            url,
            profile=get_profile(profile),
            config=config,
            wait_until=parse_wait_until(wait_until),
            timeout_ms=timeout_ms,
            text_only=text_only,
        )
    return shape_fetch_result(result, max_chars)


@mcp.tool()
async def stealth_fetch(
    url: str,
    profile: str = "desktop-chrome",
    channel: str = "chrome",
    headless: bool = True,
    user_data_dir: str | None = None,
    patchright_context_profile: bool = False,
    reduce_fingerprint_surface: bool = False,
    mask_headless_user_agent: bool = False,
    webrtc_ip_handling_policy: str | None = None,
    timeout_ms: float = 90_000,
    max_chars: int = 20_000,
    text_only: bool = True,
    decline_cookies: bool = True,
) -> dict[str, Any]:
    """Fetch a URL with the Patchright stealth driver (CDP-leak-free).

    Returns clean visible page text by default (LLM-friendly, no HTML tags).
    Set text_only=False to get the raw HTML instead. Requires Patchright's
    browser download: webskrap install. Prefer headless=False with
    channel="chrome" for the strictest anti-bot path.

    Args:
        url: The URL to load.
        profile: Bundled profile applied when patchright_context_profile is set.
        channel: Browser channel, e.g. chrome.
        headless: Run headless. Headed is more robust against detection.
        user_data_dir: Persistent browser profile directory.
        patchright_context_profile: Apply locale/timezone/media profile metadata.
        reduce_fingerprint_surface: Disable WebGL and canvas readback via flags.
        mask_headless_user_agent: Rewrite the HeadlessChrome UA token to Chrome.
        webrtc_ip_handling_policy: Chromium WebRTC ICE policy, e.g.
            disable_non_proxied_udp.
        timeout_ms: Navigation timeout in milliseconds.
        max_chars: Maximum characters of page text to return.
        text_only: Return clean visible text (default) instead of raw HTML.
        decline_cookies: Click a cookie consent notice's reject button after
            load, so the banner does not bury the page text.
    """
    config = SessionConfig(
        driver="patchright",
        channel=channel,
        headless=headless,
        user_data_dir=Path(user_data_dir) if user_data_dir else None,
        navigation_timeout_ms=timeout_ms,
        patchright_context_profile=patchright_context_profile,
        reduce_fingerprint_surface=reduce_fingerprint_surface,
        mask_headless_user_agent=mask_headless_user_agent,
        webrtc_ip_handling_policy=parse_webrtc_ip_handling_policy(webrtc_ip_handling_policy),
        decline_cookies=decline_cookies,
    )
    async with WebSkrapClient() as client:
        result = await client.fetch(
            url,
            profile=get_profile(profile),
            config=config,
            timeout_ms=timeout_ms,
            text_only=text_only,
        )
    return shape_fetch_result(result, max_chars)


@mcp.tool()
async def doctor() -> dict[str, object]:
    """Check that Patchright and Chromium are installed and can launch."""
    return await browser_doctor()


def main() -> None:
    """Entry point for the ``webskrap-mcp`` console script (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
