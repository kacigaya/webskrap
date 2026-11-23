"""Model Context Protocol server exposing WebSkrap over stdio.

Run with ``webskrap-mcp`` (after ``pip install webskrap``) or
``python -m webskrap.interfaces.mcp_server``. Point an MCP client (Claude Desktop, Claude
Code, ...) at that command to drive scraping through the tools below.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from importlib import resources
from importlib.metadata import version
from typing import Any, TypeVar

from anyio import CancelScope

from webskrap.browser import browser_session
from webskrap.browser.channels import with_channel_fallback
from webskrap.browser.diagnostics import diagnose
from webskrap.config.models import GpuBackend, SessionConfig, WaitUntil
from webskrap.config.parsing import (
    parse_engine,
    parse_profile_mode,
    parse_resource_policy,
    parse_wait_until,
    parse_webrtc_ip_handling_policy,
)
from webskrap.config.profile import BrowserProfile
from webskrap.config.profiles import get_profile, list_profiles
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.interfaces.mcp_browser import EVAL_ENV as EVAL_ENV
from webskrap.interfaces.mcp_browser import MAX_EVAL_CHARS as MAX_EVAL_CHARS
from webskrap.interfaces.mcp_browser import _browser_action as _browser_action
from webskrap.interfaces.mcp_browser import browser_close as browser_close
from webskrap.interfaces.mcp_browser import browser_eval as browser_eval
from webskrap.interfaces.mcp_browser import browser_find as browser_find
from webskrap.interfaces.mcp_browser import browser_goto as browser_goto
from webskrap.interfaces.mcp_browser import browser_insert_text as browser_insert_text
from webskrap.interfaces.mcp_browser import browser_interact as browser_interact
from webskrap.interfaces.mcp_browser import browser_list as browser_list
from webskrap.interfaces.mcp_browser import browser_mouse as browser_mouse
from webskrap.interfaces.mcp_browser import browser_open as browser_open
from webskrap.interfaces.mcp_browser import browser_press as browser_press
from webskrap.interfaces.mcp_browser import browser_screenshot as browser_screenshot
from webskrap.interfaces.mcp_browser import browser_snapshot as browser_snapshot
from webskrap.interfaces.mcp_browser import browser_solve_captcha as browser_solve_captcha
from webskrap.interfaces.mcp_browser import browser_tabs as browser_tabs
from webskrap.interfaces.mcp_browser import browser_text as browser_text
from webskrap.interfaces.mcp_browser import browser_view as browser_view
from webskrap.interfaces.mcp_browser import browser_wait_for as browser_wait_for
from webskrap.interfaces.mcp_browser import (
    close_owned_sessions,
    idle_timeout_s,
    reap_idle_sessions,
    register_browser_tools,
)
from webskrap.interfaces.mcp_support import Context, MCPServer, _hints, _tool_errors
from webskrap.interfaces.mcp_support import _AnnotatedError as _AnnotatedError
from webskrap.scraping.client import WebSkrapClient
from webskrap.scraping.fetch_runtime import FetchRuntime
from webskrap.scraping.results import (
    FetchResult,
    SearchResult,
    shape_fetch_result,
    shape_search_result,
)
from webskrap.security.paths import resolve_mcp_profile_path
from webskrap.security.urls import validate_mcp_url

logger = logging.getLogger("webskrap.mcp_server")


T = TypeVar("T")


INSTRUCTIONS = """WebSkrap drives real Chromium over its own CDP client (no Playwright).

Choose a tool
- One known page: stealth_fetch. Prefer it over fetch; fetch has fewer controls.
- search: find URLs (Bing default, engine="ddg" for DuckDuckGo). No Google.
  Then stealth_fetch selected hits. On blocked, switch engine or exit IP.
- Stateful flows: browser_open, browser_snapshot / browser_interact /
  browser_wait_for, then browser_close. Do not re-fetch to drive a flow.

In a browser session
- No visible window; virtual_display=True is not headless.
  Shared with the browser CLI. browser_tabs picks the tab.
- browser_find(text=...) locates known controls with refs and ancestor context.
  browser_snapshot(target=..., depth=...) scopes a tree; boxes=true adds geometry.
- browser_text reads text without an accessibility tree; pattern= greps lines.
- Visual layouts/canvas: browser_view returns an inline PNG and metadata,
  without a file write. browser_mouse clicks/moves/scrolls/drags in viewport
  CSS pixels from its top-left (0,0). Map resized previews to original width/
  height. Take a fresh view after scrolling, navigation, or layout changes.
- browser_insert_text inserts literal text into the focused control;
  browser_press sends keys. Prefer refs/selectors for identifiable controls.
- Use browser_wait_for after actions. Run dependent actions in sequence.

Keep results small
- max_chars defaults to 8000. Start lower; page via next_text_offset /
  next_snapshot_offset. browser_snapshot defaults to depth=6 (null for full).
- Keep text_only=true; browser_eval should return a narrow value, not full DOM.
- Fetch defaults: domcontentloaded, resource_policy="all" ("lite" is faster for
  text). Use ready_selector for deferred content; networkidle only if needed.
- Opt into repeated fetch reuse with session="crawl"; cookies/storage share that
  name. Keep profile/config unchanged. fetch_session_close releases it.
  These warm sessions are separate from browser_* sessions and end on server
  shutdown. At most eight fetches run at once and eight warm sessions stay open.

Failures and boundaries
Errors include a code and recovery hint. no_session: browser_open first;
stale_ref: snapshot again; timeout: raise timeout_ms or weaken load state;
browser_launch: `webskrap install`. Fetch/search fall back from chrome to Chromium.
Known challenge/block pages return blocked: follow its hint; do not retry.
browser_goto reports it; solve_*=true passes it.
browser_solve_captcha ticks a checkbox.
Call doctor for unexplained failures; read webskrap://guide for details.
Screenshots are confined to ./webskrap-output, profiles to ~/.webskrap/profiles.
Paths must be relative and cannot escape their roots (path_rejected).
Targets must be public http(s): local/private hosts need operator-set
WEBSKRAP_ALLOW_PRIVATE_NET=1. No login-wall bypass.
Prefer snapshot/interact/wait_for to browser_eval, and never evaluate
page-controlled text. WEBSKRAP_ALLOW_EVAL=0 disables page script evaluation.
"""


@asynccontextmanager
async def _fetch_lifespan(_server: MCPServer) -> AsyncIterator[FetchRuntime]:
    idle_s = idle_timeout_s()
    runtime = FetchRuntime()
    reaper = asyncio.ensure_future(reap_idle_sessions(idle_s)) if idle_s else None
    try:
        yield runtime
    finally:
        with CancelScope(shield=True):
            try:
                if reaper is not None:
                    reaper.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await reaper
                await close_owned_sessions()
            finally:
                await runtime.close()


mcp = MCPServer(
    "webskrap", instructions=INSTRUCTIONS, version=version("webskrap"), lifespan=_fetch_lifespan
)


def _log_channel_retry(failed: str | None, retry: str | None) -> None:
    logger.warning("channel %r did not launch; retrying with %r", failed, retry)


async def _run_fetch(
    url: str,
    *,
    config: SessionConfig,
    profile: BrowserProfile,
    wait_until: WaitUntil,
    timeout_ms: float,
    text_only: bool,
    include_links: bool,
    max_links: int,
    session: str | None,
    ctx: Context | None,
    ready_selector: str | None = None,
) -> FetchResult:
    if ctx is None and session is not None:
        raise WebSkrapError("fetch sessions require the MCP lifespan", code=ErrorCode.USAGE)
    runtime = None if ctx is None else ctx.request_context.lifespan_context
    if ctx is not None and not isinstance(runtime, FetchRuntime):
        raise WebSkrapError("fetch runtime is unavailable", code=ErrorCode.USAGE)

    async def attempt(resolved: SessionConfig) -> FetchResult:
        fetch_options: dict[str, Any] = {
            "wait_until": wait_until,
            "timeout_ms": timeout_ms,
            "text_only": text_only,
            "include_links": include_links,
            "max_links": max_links,
            "decline_cookies": resolved.decline_cookies,
            "ready_selector": ready_selector,
            "solve_cloudflare": resolved.solve_cloudflare,
            "solve_datadome": resolved.solve_datadome,
            "solve_perimeterx": resolved.solve_perimeterx,
            "solve_recaptcha": resolved.solve_recaptcha,
        }
        if not isinstance(runtime, FetchRuntime):
            # Direct Python tool calls retain their one-shot lifecycle.
            async with WebSkrapClient() as client:
                return await client.fetch(url, config=resolved, profile=profile, **fetch_options)
        async with runtime.use(session, resolved, profile) as target:
            return await target.fetch(url, **fetch_options)

    return await with_channel_fallback(
        attempt, config, on_retry=_log_channel_retry, exclusive=session is not None
    )


def _stealth_config(
    *,
    channel: str,
    headless: bool,
    user_data_dir: str | None,
    timeout_ms: float,
    profile_mode: str,
    reduce_fingerprint_surface: bool,
    mask_headless_user_agent: bool,
    virtual_display: bool,
    gpu: GpuBackend,
    fake_media_devices: bool,
    webrtc_ip_handling_policy: str | None,
    decline_cookies: bool,
    resource_policy: str = "auto",
    solve_cloudflare: bool = False,
    solve_datadome: bool = False,
    solve_perimeterx: bool = False,
    solve_recaptcha: bool = False,
) -> SessionConfig:
    """Build the session config the stealth_fetch and search tools share."""
    return SessionConfig(
        channel=channel,
        headless=headless,
        chromium_sandbox=browser_session.sandbox_enabled(None),
        user_data_dir=resolve_mcp_profile_path(user_data_dir) if user_data_dir else None,
        navigation_timeout_ms=timeout_ms,
        profile_mode=parse_profile_mode(profile_mode),
        reduce_fingerprint_surface=reduce_fingerprint_surface,
        mask_headless_user_agent=mask_headless_user_agent,
        virtual_display=virtual_display,
        gpu=gpu,
        fake_media_devices=fake_media_devices,
        webrtc_ip_handling_policy=parse_webrtc_ip_handling_policy(webrtc_ip_handling_policy),
        decline_cookies=decline_cookies,
        resource_policy=parse_resource_policy(resource_policy),
        solve_cloudflare=solve_cloudflare,
        solve_datadome=solve_datadome,
        solve_perimeterx=solve_perimeterx,
        solve_recaptcha=solve_recaptcha,
    )


@mcp.tool(title="Fetch a page", annotations=_hints(read_only=True, open_world=True))
async def fetch(
    url: str,
    profile: str = "desktop-chrome",
    channel: str = "chrome",
    wait_until: str = "domcontentloaded",
    resource_policy: str = "all",
    timeout_ms: float = 60_000,
    max_chars: int = 8_000,
    offset: int = 0,
    text_only: bool = True,
    include_links: bool = False,
    max_links: int = 50,
    decline_cookies: bool = True,
    *,
    ready_selector: str | None = None,
    solve_cloudflare: bool = False,
    solve_datadome: bool = False,
    solve_perimeterx: bool = False,
    solve_recaptcha: bool = False,
    session: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Fetch a URL in headless Chromium and return page data.

    Uses the same stealth path as the CLI (no Runtime.enable, isolated-world
    scripts), so
    JS-heavy and anti-bot pages that block naive scrapers still load. Uses
    domcontentloaded by default; set ready_selector for deferred content.
    Returns clean visible page text by default (LLM-friendly, no HTML tags); set
    text_only=False to get the raw HTML instead. For finer stealth control
    (fingerprint surface, WebRTC, UA masking, persistent profile) use
    stealth_fetch.

    Args:
        url: The URL to load.
        profile: Profile label; the browser keeps its native identity here.
        channel: Browser channel, e.g. chrome. If chrome cannot launch (Linux
            ARM64 has no build), Edge and then bundled Chromium are tried.
        wait_until: commit, domcontentloaded, load, or networkidle.
        resource_policy: all (default, as in stealth_fetch, so both can share
            a warm session), auto (lite for text, all for HTML), lite, or
            documents (also blocks CSS).
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
        ready_selector: Optional CSS selector or snapshot ref that must be visible
            before extraction. Uses timeout_ms after consent dismissal.
        solve_cloudflare: Click Cloudflare's Turnstile checkbox on its "Just a
            moment" page like a person and wait for the real page, for up to
            60 s beyond timeout_ms. Off by default. Use responsibly.
        solve_datadome: Drag DataDome's slider CAPTCHA like a person and
            wait for the real page, for up to 60 s beyond timeout_ms. Off by
            default. Use responsibly.
        solve_perimeterx: Hold HUMAN's (PerimeterX) Press & Hold button like
            a person and wait for the real page, for up to 60 s beyond
            timeout_ms. Off by default. Use responsibly.
        solve_recaptcha: Click a reCAPTCHA v2 or hCaptcha checkbox on a 403,
            429 or 503 page and wait up to 60 s beyond timeout_ms for the real
            page. Use responsibly.
        session: Opt-in warm fetch session. Shares cookies/storage across calls;
            keep profile/config identical and close with fetch_session_close.
        ctx: Server-injected request context.
    """
    return await stealth_fetch(
        url,
        profile=profile,
        channel=channel,
        timeout_ms=timeout_ms,
        max_chars=max_chars,
        offset=offset,
        text_only=text_only,
        include_links=include_links,
        max_links=max_links,
        decline_cookies=decline_cookies,
        ready_selector=ready_selector,
        wait_until=wait_until,
        resource_policy=resource_policy,
        solve_cloudflare=solve_cloudflare,
        solve_datadome=solve_datadome,
        solve_perimeterx=solve_perimeterx,
        solve_recaptcha=solve_recaptcha,
        session=session,
        ctx=ctx,
    )


@mcp.tool(title="Stealth-fetch a page", annotations=_hints(read_only=True, open_world=True))
async def stealth_fetch(
    url: str,
    profile: str = "desktop-chrome",
    channel: str = "chrome",
    headless: bool = True,
    user_data_dir: str | None = None,
    profile_mode: str = "native",
    reduce_fingerprint_surface: bool = False,
    mask_headless_user_agent: bool = False,
    virtual_display: bool = False,
    gpu: GpuBackend = GpuBackend.AUTO,
    fake_media_devices: bool = False,
    webrtc_ip_handling_policy: str | None = None,
    timeout_ms: float = 90_000,
    max_chars: int = 8_000,
    offset: int = 0,
    text_only: bool = True,
    include_links: bool = False,
    max_links: int = 50,
    decline_cookies: bool = True,
    *,
    ready_selector: str | None = None,
    wait_until: str = "domcontentloaded",
    resource_policy: str = "all",
    solve_cloudflare: bool = False,
    solve_datadome: bool = False,
    solve_perimeterx: bool = False,
    solve_recaptcha: bool = False,
    session: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Fetch a URL in Chromium with every stealth control exposed.

    Returns clean visible page text by default (LLM-friendly, no HTML tags).
    Set text_only=False to get the raw HTML instead. Needs a browser: run
    webskrap install for Chrome for Testing. Prefer virtual_display=True, or
    headless=False with channel="chrome", for the strictest anti-bot path.

    Args:
        url: The URL to load.
        profile: Bundled profile; profile_mode decides how much pages see.
        channel: Browser channel, e.g. chrome. If chrome cannot launch (Linux
            ARM64 has no build), Edge and then bundled Chromium are tried.
        headless: Run headless. Headed is more robust against detection.
        user_data_dir: Persistent browser profile directory, relative to the
            MCP profile root.
        profile_mode: native (default; the browser's own identity), locale
            (locale, timezone, media, headers) or full (also viewport and UA).
        reduce_fingerprint_surface: Disable WebGL and canvas readback via flags.
        mask_headless_user_agent: Rewrite the HeadlessChrome UA token to Chrome.
            Empties high-entropy client hints; prefer virtual_display.
        virtual_display: Run headed Chromium on a private Xvfb display
            instead of headless (Linux with Xvfb). No HeadlessChrome token
            and full client hints, without any override.
        gpu: WebGL renderer. "auto" is Chromium's choice (SwiftShader
            without a GPU, a known headless tell), and "mesa" under
            virtual_display without a GPU when lavapipe is installed; "mesa"
            uses Mesa's lavapipe (Linux with mesa-vulkan-drivers).
        fake_media_devices: Provide Chromium's synthetic camera and microphone.
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
        wait_until: commit, domcontentloaded (default), load, or networkidle.
        resource_policy: all (default: page scripts can see a blocked image
            or font), auto (lite for text, all for HTML), lite, or documents
            (also blocks CSS).
        ready_selector: Optional CSS selector or snapshot ref that must be visible
            before extraction. Uses timeout_ms after consent dismissal.
        solve_cloudflare: Click Cloudflare's Turnstile checkbox on its "Just a
            moment" page like a person and wait for the real page, for up to
            60 s beyond timeout_ms. Off by default. Use responsibly.
        solve_datadome: Drag DataDome's slider CAPTCHA like a person and
            wait for the real page, for up to 60 s beyond timeout_ms. Off by
            default. Use responsibly.
        solve_perimeterx: Hold HUMAN's (PerimeterX) Press & Hold button like
            a person and wait for the real page, for up to 60 s beyond
            timeout_ms. Off by default. Use responsibly.
        solve_recaptcha: Click a reCAPTCHA v2 or hCaptcha checkbox on a 403,
            429 or 503 page and wait up to 60 s beyond timeout_ms for the real
            page. Use responsibly.
        session: Opt-in warm fetch session. Shares cookies/storage across calls;
            keep profile/config identical and close with fetch_session_close.
        ctx: Server-injected request context.
    """
    with _tool_errors():
        url = await validate_mcp_url(url)
        config = _stealth_config(
            channel=channel,
            headless=headless,
            user_data_dir=user_data_dir,
            timeout_ms=timeout_ms,
            profile_mode=profile_mode,
            reduce_fingerprint_surface=reduce_fingerprint_surface,
            mask_headless_user_agent=mask_headless_user_agent,
            virtual_display=virtual_display,
            gpu=gpu,
            fake_media_devices=fake_media_devices,
            webrtc_ip_handling_policy=webrtc_ip_handling_policy,
            decline_cookies=decline_cookies,
            resource_policy=resource_policy,
            solve_cloudflare=solve_cloudflare,
            solve_datadome=solve_datadome,
            solve_perimeterx=solve_perimeterx,
            solve_recaptcha=solve_recaptcha,
        )
        result = await _run_fetch(
            url,
            profile=get_profile(profile),
            config=config,
            wait_until=parse_wait_until(wait_until),
            timeout_ms=timeout_ms,
            text_only=text_only,
            include_links=include_links,
            max_links=max_links,
            session=session,
            ctx=ctx,
            ready_selector=ready_selector,
        )
        return shape_fetch_result(result, max_chars, offset)


@mcp.tool(
    title="Close a warm fetch session",
    annotations=_hints(read_only=False, destructive=True, idempotent=True),
)
async def fetch_session_close(session: str, ctx: Context) -> dict[str, bool]:
    """Release a named fetch session's browser and temporary cookies/storage.

    Separate from browser_* sessions. Persistent user_data_dir files survive.
    A busy session is refused; retry after its fetches finish.
    """
    with _tool_errors():
        runtime = ctx.request_context.lifespan_context
        if not isinstance(runtime, FetchRuntime):
            raise WebSkrapError("fetch runtime is unavailable", code=ErrorCode.USAGE)
        return {"closed": await runtime.close_session(session)}


@mcp.tool(title="Search the web", annotations=_hints(read_only=True, open_world=True))
async def search(
    query: str,
    engine: str = "bing",
    max_results: int = 10,
    profile: str = "desktop-chrome",
    channel: str = "chrome",
    headless: bool = True,
    user_data_dir: str | None = None,
    profile_mode: str = "native",
    reduce_fingerprint_surface: bool = False,
    mask_headless_user_agent: bool = False,
    virtual_display: bool = False,
    gpu: GpuBackend = GpuBackend.AUTO,
    fake_media_devices: bool = False,
    webrtc_ip_handling_policy: str | None = None,
    timeout_ms: float = 90_000,
    decline_cookies: bool = True,
) -> dict[str, Any]:
    """Search the web and return the organic hits as title, url and snippet.

    Loads the engine's own results page in Chromium, so the same
    channel, profile and persistent-profile controls as stealth_fetch apply.
    Returns destinations with the engine's click-tracking unwrapped; follow up
    with stealth_fetch on the ones worth reading. Google is not offered. A
    `blocked` error means the engine served a bot challenge instead of
    results: try the other engine, a persistent user_data_dir, or another
    exit IP rather than retrying unchanged.

    Args:
        query: Words to search for.
        engine: bing (default) or ddg (DuckDuckGo).
        max_results: How many hits to return; hits_total counts them all.
        profile: Bundled profile; profile_mode decides how much pages see.
        channel: Browser channel, e.g. chrome. If chrome cannot launch (Linux
            ARM64 has no build), Edge and then bundled Chromium are tried.
        headless: Run headless. Headed is more robust against detection.
        user_data_dir: Persistent browser profile directory, relative to the
            MCP profile root.
        profile_mode: native (default; the browser's own identity), locale
            (locale, timezone, media, headers) or full (also viewport and UA).
        reduce_fingerprint_surface: Disable WebGL and canvas readback via flags.
        mask_headless_user_agent: Rewrite the HeadlessChrome UA token to Chrome.
            Empties high-entropy client hints; prefer virtual_display.
        virtual_display: Run headed Chromium on a private Xvfb display
            instead of headless (Linux with Xvfb). No HeadlessChrome token
            and full client hints, without any override.
        gpu: WebGL renderer. "auto" is Chromium's choice (SwiftShader
            without a GPU, a known headless tell), and "mesa" under
            virtual_display without a GPU when lavapipe is installed; "mesa"
            uses Mesa's lavapipe (Linux with mesa-vulkan-drivers).
        fake_media_devices: Provide Chromium's synthetic camera and microphone.
        webrtc_ip_handling_policy: Chromium WebRTC ICE policy, e.g.
            disable_non_proxied_udp.
        timeout_ms: Navigation timeout in milliseconds.
        decline_cookies: Click a cookie consent notice's reject button on the
            results page before reading it.
    """
    with _tool_errors():
        config = _stealth_config(
            channel=channel,
            headless=headless,
            user_data_dir=user_data_dir,
            timeout_ms=timeout_ms,
            profile_mode=profile_mode,
            reduce_fingerprint_surface=reduce_fingerprint_surface,
            mask_headless_user_agent=mask_headless_user_agent,
            virtual_display=virtual_display,
            gpu=gpu,
            fake_media_devices=fake_media_devices,
            webrtc_ip_handling_policy=webrtc_ip_handling_policy,
            decline_cookies=decline_cookies,
        )
        selected_engine = parse_engine(engine)
        selected_profile = get_profile(profile)

        async def attempt(resolved: SessionConfig) -> SearchResult:
            async with WebSkrapClient() as client:
                return await client.search(
                    query,
                    engine=selected_engine,
                    max_results=max_results,
                    profile=selected_profile,
                    config=resolved,
                    timeout_ms=timeout_ms,
                )

        result = await with_channel_fallback(attempt, config, on_retry=_log_channel_retry)
        return shape_search_result(result)


@mcp.tool(title="Check WebSkrap readiness", annotations=_hints(read_only=True, idempotent=True))
async def doctor() -> dict[str, Any]:
    """Report whether WebSkrap can drive a browser here, and how it is configured.

    Launches one headless Chromium and returns that result plus the installed
    versions, the Chromium binary in use, the roots screenshots and profiles
    are confined to, which WEBSKRAP_* overrides are set, and every persistent
    browser session with its running state. fingerprint lists what a default
    fetch shows pages; warnings name a native fix for each uncommon value.
    Call this first when a fetch or a browser_* tool fails for a reason its
    own error does not explain.
    """
    with _tool_errors():
        return await diagnose()


register_browser_tools(mcp)


def _guide() -> str:
    """Read the packaged guide.

    Kept as a data file rather than a string constant: it is a document, and
    inlining it would push markdown tables through the source line-length rules
    for no benefit.
    """
    return resources.files("webskrap.interfaces").joinpath("guide.md").read_text(encoding="utf-8")


@mcp.resource(
    "webskrap://guide",
    name="webskrap_guide",
    title="WebSkrap usage guide",
    description=(
        "Which tool to use for which goal, what each returns, how to keep results small, "
        "and what to do about each error code."
    ),
    mime_type="text/markdown",
)
def guide_resource() -> str:
    """Return the long-form guide the short server instructions point at."""
    return _guide()


@mcp.resource(
    "webskrap://profiles",
    name="webskrap_profiles",
    title="Bundled browser profiles",
    description="The profile names fetch and stealth_fetch accept, with their settings.",
    mime_type="application/json",
)
def profiles_resource() -> str:
    """Return the bundled profiles, so listing them costs no tool call."""
    return json.dumps(
        {"profiles": [profile.model_dump(mode="json") for profile in list_profiles()]},
        ensure_ascii=False,
    )


@mcp.resource(
    "webskrap://sessions",
    name="webskrap_sessions",
    title="Persistent browser sessions",
    description="Every on-disk browser session and whether its browser is running.",
    mime_type="application/json",
)
def sessions_resource() -> str:
    """Return the same inventory as browser_list, readable without a tool call."""
    return json.dumps({"sessions": browser_session.list_sessions()}, ensure_ascii=False)


def main() -> None:
    """Entry point for the ``webskrap-mcp`` console script (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
