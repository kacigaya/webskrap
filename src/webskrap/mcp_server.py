"""Model Context Protocol server exposing WebSkrap over stdio.

Run with ``webskrap-mcp`` (after ``pip install webskrap``) or
``python -m webskrap.mcp_server``. Point an MCP client (Claude Desktop, Claude
Code, ...) at that command to drive scraping through the tools below.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from playwright.async_api import Page

from webskrap import browser_session
from webskrap.client import WebSkrapClient, WebSkrapError, browser_doctor
from webskrap.models import SessionConfig, shape_fetch_result
from webskrap.parsing import (
    parse_resource_policy,
    parse_wait_until,
    parse_webrtc_ip_handling_policy,
)
from webskrap.paths import resolve_mcp_profile_path, resolve_output_path
from webskrap.profiles import get_profile

T = TypeVar("T")

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
    offset: int = 0,
    text_only: bool = True,
    include_links: bool = False,
    max_links: int = 50,
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
        offset: Character index to start the returned text at. Pass back the
            result's next_text_offset to read a long page in windows instead of
            fetching it again with a bigger max_chars.
        text_only: Return clean visible text (default) instead of raw HTML.
        include_links: Also return the page's outbound links, so a crawl does
            not need a second fetch of the raw HTML. Off by default: on a
            link-heavy page they cost more than the text.
        max_links: How many links to return; links_total counts them all.
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
            include_links=include_links,
            max_links=max_links,
        )
    return shape_fetch_result(result, max_chars, offset)


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
    offset: int = 0,
    text_only: bool = True,
    include_links: bool = False,
    max_links: int = 50,
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
        user_data_dir: Persistent browser profile directory, relative to the
            MCP profile root.
        patchright_context_profile: Apply locale/timezone/media profile metadata.
        reduce_fingerprint_surface: Disable WebGL and canvas readback via flags.
        mask_headless_user_agent: Rewrite the HeadlessChrome UA token to Chrome.
        webrtc_ip_handling_policy: Chromium WebRTC ICE policy, e.g.
            disable_non_proxied_udp.
        timeout_ms: Navigation timeout in milliseconds.
        max_chars: Maximum characters of page text to return.
        offset: Character index to start the returned text at; pass back
            next_text_offset to continue.
        text_only: Return clean visible text (default) instead of raw HTML.
        include_links: Also return the page's outbound links.
        max_links: How many links to return; links_total counts them all.
        decline_cookies: Click a cookie consent notice's reject button after
            load, so the banner does not bury the page text.
    """
    config = SessionConfig(
        driver="patchright",
        channel=channel,
        headless=headless,
        user_data_dir=resolve_mcp_profile_path(user_data_dir) if user_data_dir else None,
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
            include_links=include_links,
            max_links=max_links,
        )
    return shape_fetch_result(result, max_chars, offset)


@mcp.tool()
async def doctor() -> dict[str, object]:
    """Check that Patchright and Chromium are installed and can launch."""
    return await browser_doctor()


async def _browser_action(
    session: str,
    action: Callable[[Page], Awaitable[T]],
    timeout_ms: float,
) -> T:
    """Run a page action, flattening Playwright errors to one-line messages."""
    try:
        return await browser_session.run_page_action(session, action, timeout_ms=timeout_ms)
    except WebSkrapError:
        raise
    except Exception as exc:
        raise WebSkrapError(str(exc).strip().splitlines()[0] or type(exc).__name__) from exc


@mcp.tool()
async def browser_open(
    url: str | None = None,
    session: str = "default",
) -> dict[str, Any]:
    """Start (or reuse) a persistent headless browser session.

    The browser is a detached Chromium that keeps running between tool calls
    (and between MCP server restarts); every browser_* tool reconnects to it
    over CDP. The session's profile persists on disk (owner-readable only), so
    cookies and logins survive close/open. Shares sessions with the
    `webskrap browser` CLI.

    The browser keeps Chromium's OS sandbox. Environments that cannot sandbox
    must set WEBSKRAP_CHROMIUM_SANDBOX=0 before starting the server; the switch
    is deliberately not a tool argument, so a page cannot talk the model into
    weakening renderer isolation.

    Args:
        url: Optional URL to open after launch.
        session: Session name; letters, digits, '.', '_' or '-'.
    """
    payload = await browser_session.open_session(session, headless=True)
    if url:
        payload.update(
            await _browser_action(
                session,
                lambda page: browser_session.goto(page, url, "load"),
                browser_session.DEFAULT_NAVIGATION_TIMEOUT_MS,
            )
        )
    return payload


@mcp.tool()
async def browser_goto(
    url: str,
    session: str = "default",
    wait_until: str = "load",
    timeout_ms: float = 30_000,
) -> dict[str, Any]:
    """Navigate the session's current page to a URL.

    Args:
        url: The URL to navigate to.
        session: Browser session name.
        wait_until: commit, domcontentloaded, load, or networkidle.
        timeout_ms: Navigation timeout in milliseconds.
    """
    parsed_wait_until = parse_wait_until(wait_until)
    return await _browser_action(
        session,
        lambda page: browser_session.goto(page, url, parsed_wait_until),
        timeout_ms,
    )


@mcp.tool()
async def browser_snapshot(
    session: str = "default",
    depth: int | None = None,
    max_chars: int = 20_000,
) -> dict[str, Any]:
    """Return an aria snapshot of the current page with eN element refs.

    Each element carries a ref like [ref=e15]; pass that ref (e.g. "e15") as
    the target of browser_interact. Refs describe the current DOM, so take a
    fresh snapshot after the page changes.

    Args:
        session: Browser session name.
        depth: Maximum snapshot tree depth.
        max_chars: Maximum characters of snapshot text to return.
    """
    result = await _browser_action(
        session,
        lambda page: browser_session.snapshot(page, depth=depth),
        browser_session.DEFAULT_ACTION_TIMEOUT_MS,
    )
    truncated = len(result["snapshot"]) > max_chars
    if truncated:
        result["snapshot"] = result["snapshot"][:max_chars]
    result["snapshot_truncated"] = truncated
    return result


@mcp.tool()
async def browser_interact(
    action: str,
    target: str,
    values: list[str] | None = None,
    session: str = "default",
    timeout_ms: float = 10_000,
) -> dict[str, Any]:
    """Interact with an element on the session's current page.

    Args:
        action: click, dblclick, hover, check, uncheck, fill, type, or select.
        target: Snapshot ref (e.g. "e15") or any Playwright selector.
        values: Value arguments: none for click/dblclick/hover/check/uncheck,
            exactly one for fill/type, one or more for select.
        session: Browser session name.
        timeout_ms: Action timeout in milliseconds.
    """
    resolved_values = values or []
    browser_session.element_arguments(action, resolved_values)

    async def run(page: Page) -> dict[str, Any]:
        await browser_session.element_action(page, action, target, resolved_values)
        return await browser_session.page_state(page)

    return await _browser_action(session, run, timeout_ms)


@mcp.tool()
async def browser_press(
    key: str,
    session: str = "default",
    timeout_ms: float = 10_000,
) -> dict[str, Any]:
    """Press a keyboard key on the session's current page.

    Args:
        key: Key to press, e.g. Enter, Tab, or Control+a.
        session: Browser session name.
        timeout_ms: Action timeout in milliseconds.
    """

    async def run(page: Page) -> dict[str, Any]:
        await page.keyboard.press(key)
        return await browser_session.page_state(page)

    return await _browser_action(session, run, timeout_ms)


@mcp.tool()
async def browser_screenshot(
    path: str | None = None,
    session: str = "default",
    full_page: bool = False,
) -> dict[str, Any]:
    """Screenshot the session's current page to a PNG file.

    Screenshots are written under ./webskrap-output (override with the
    WEBSKRAP_OUTPUT_DIR environment variable). Absolute paths and paths that
    escape that directory are rejected.

    Args:
        path: Output PNG path relative to the output directory; nested
            subdirectories are allowed and created. Auto-generated when omitted.
        session: Browser session name.
        full_page: Capture the full scrollable page.
    """
    # Resolved before connecting to the browser so a rejected path fails fast,
    # and outside `run` so no page action happens for a path that is refused.
    target = resolve_output_path(path)

    async def run(page: Page) -> dict[str, Any]:
        await page.screenshot(path=str(target), full_page=full_page)
        return {**await browser_session.page_state(page), "path": str(target)}

    return await _browser_action(session, run, browser_session.DEFAULT_ACTION_TIMEOUT_MS)


@mcp.tool()
async def browser_eval(
    expression: str,
    session: str = "default",
    timeout_ms: float = 10_000,
) -> dict[str, Any]:
    """Evaluate JavaScript on the session's current page.

    Args:
        expression: JavaScript expression or function to evaluate.
        session: Browser session name.
        timeout_ms: Action timeout in milliseconds.
    """
    result = await _browser_action(session, lambda page: page.evaluate(expression), timeout_ms)
    return {"result": result}


@mcp.tool()
async def browser_close(
    session: str = "default",
    delete_data: bool = False,
    all_sessions: bool = False,
) -> dict[str, Any]:
    """Close a browser session (its profile persists unless delete_data).

    Args:
        session: Browser session name; ignored when all_sessions is set.
        delete_data: Also delete the session's on-disk profile data.
        all_sessions: Close every session.
    """
    names = browser_session.list_session_names() if all_sessions else [session]
    closed = []
    for name in names:
        closed.append(
            await asyncio.to_thread(browser_session.close_session, name, delete_data=delete_data)
        )
    return {"closed": closed}


@mcp.tool()
async def browser_list() -> dict[str, Any]:
    """List persistent browser sessions and whether each is running."""
    return {"sessions": browser_session.list_sessions()}


def main() -> None:
    """Entry point for the ``webskrap-mcp`` console script (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
