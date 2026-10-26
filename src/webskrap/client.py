"""Async scraping client: Chromium sessions, fetches, searches and clicks.

:class:`WebSkrapClient` hands out :class:`WebSkrapSession` objects; a session
owns one Chromium driven over WebSkrap's own CDP client (:mod:`webskrap.cdp`)
and the pages opened in it. Both are async context managers, and both raise
:class:`WebSkrapError` for every failure, with an :class:`ErrorCode` that
says what to do next.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from anyio import CancelScope

from webskrap import cdp_dom, cdp_setup
from webskrap.cdp import CDPBrowser, CDPError, CDPPage, find_executable, launch
from webskrap.challenges import (
    raise_for_challenge,
    record_documents,
    response_headers,
    response_status,
    settle,
)
from webskrap.consent import SETTLED_PAGE_TIMEOUT_MS
from webskrap.consent import decline_cookies as _decline_cookies
from webskrap.display import VirtualDisplay
from webskrap.errors import (
    RECOVERY_HINTS,
    ErrorCode,
    WebSkrapError,
    is_launch_failure,
    is_sandbox_failure,
)
from webskrap.models import (
    BrowserProfile,
    FetchResult,
    GpuBackend,
    Link,
    SearchEngine,
    SearchResult,
    SessionConfig,
    WaitUntil,
)
from webskrap.profiles import get_profile
from webskrap.search import parse_results, search_url
from webskrap.urls import validate_url

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Collects every anchor's resolved absolute URL once, in document order, and
# returns the first ``max`` of them plus the unique total. Deduplicating in the
# page keeps a nav bar repeated in a footer from filling the whole budget, and
# `a.href` (not getAttribute) resolves relative paths against the final URL.
LINKS_SCRIPT = """(max) => {
  const seen = new Set();
  const links = [];
  for (const anchor of document.querySelectorAll('a[href]')) {
    const href = anchor.href;
    if (!href || href.startsWith('javascript:') || seen.has(href)) continue;
    seen.add(href);
    if (links.length >= Math.max(0, max)) continue;
    links.push({
      href,
      text: (anchor.innerText || anchor.textContent || '')
        .replace(/\\s+/g, ' ')
        .trim()
        .slice(0, 120),
    });
  }
  return { links, total: seen.size };
}"""

# Matches Playwright's page.content(): doctype plus the serialized document.
_CONTENT_SCRIPT = """() => {
  let html = '';
  if (document.doctype) html = new XMLSerializer().serializeToString(document.doctype);
  if (document.documentElement) html += document.documentElement.outerHTML;
  return html;
}"""
_STATE_SCRIPT = (
    "({ url: location.href, title: document.title, "
    "text: document.body ? document.body.innerText : '' })"
)
_POLL_S = 0.1


async def browser_doctor(
    channels: tuple[str | None, ...] = ("chrome", "msedge", "chromium"),
    *,
    chromium_sandbox: bool = True,
) -> dict[str, object]:
    """Report the first Chromium channel that launches headless.

    Also reports the binary it found, since a caller diagnosing a failed
    launch otherwise has to guess where the browser was looked for.
    ``chromium_sandbox`` should match what fetches will use, or a host that
    cannot sandbox reports ready while every fetch fails.
    """
    failure: Exception | None = None
    executable_path: str | None = None
    args = [] if chromium_sandbox else ["--no-sandbox"]
    for channel in channels:
        try:
            executable = find_executable(channel)
            executable_path = str(executable)
            browser = await launch(executable, args=args, headless=True)
        except Exception as exc:  # noqa: BLE001 - report launch failures
            failure = exc
            if is_sandbox_failure(exc):
                break
            continue
        try:
            version = (await browser.connection.send("Browser.getVersion")).get("product")
        finally:
            await browser.close()
        channel_name = channel or "chromium"
        return {
            "ok": True,
            "message": f"Headless {channel_name} is ready ({version}).",
            "channel": channel_name,
            "browser_identity": {
                "chrome": "Chrome",
                "msedge": "Edge",
                "chromium": "Chromium",
            }.get(channel_name, channel_name),
            "browser_version": version,
            "executable_path": executable_path,
        }
    return {
        "ok": False,
        "message": f"Chromium did not launch: {failure}",
        "channel": None,
        "browser_identity": None,
        "browser_version": None,
        "executable_path": executable_path,
        "hint": RECOVERY_HINTS[
            ErrorCode.SANDBOX
            if failure is not None and is_sandbox_failure(failure)
            else ErrorCode.BROWSER_LAUNCH
        ],
    }


# Channel that actually launched for each requested one, including the
# requested channel itself when it works. Remembered for the process so later
# calls skip the failed launch, and so a warm MCP fetch session keeps the same
# config on every call instead of tripping its changed-options check.
_resolved_channels: dict[str | None, str | None] = {}
# Serializes exclusive calls for a requested channel until its channel is
# known, so concurrent first calls on one named fetch session never try
# different channels against the same session entry. Deliberate limit: those
# cold-start calls wait for the first one to finish, once per process.
_resolution_locks: dict[str | None, asyncio.Lock] = {}


def _fallback_channels(channel: str | None) -> tuple[str | None, ...]:
    if channel in (None, "chromium"):
        return (channel,)
    if channel == "chrome":
        return (channel, "msedge", "chromium")
    return (channel, "chromium")


async def with_channel_fallback(
    run: Callable[[SessionConfig], Awaitable[T]],
    config: SessionConfig,
    *,
    on_retry: Callable[[str | None, str | None], None] | None = None,
    exclusive: bool = False,
) -> T:
    """Run ``run(config)``, trying Edge and bundled Chromium when Chrome cannot launch.

    The default channel is ``chrome``, which does not exist on every platform
    (Linux ARM64 has no Chrome build). An installed Edge is tried before the
    bundled Chromium. Sandbox failures and non-launch errors are raised at
    once; when every channel fails to launch, the last failure is raised.

    Args:
        run: Launches a browser with the given config and does the work.
        config: Requested config; only ``channel`` changes between attempts.
        on_retry: Called with the failed and next channel before each retry.
        exclusive: Resolve the channel one call at a time. Needed when calls
            share state keyed by config, such as a named warm fetch session.
    """
    requested = config.channel
    if len(_fallback_channels(requested)) == 1:
        return await run(config)
    if exclusive and requested not in _resolved_channels:
        async with _resolution_locks.setdefault(requested, asyncio.Lock()):
            if requested not in _resolved_channels:
                return await _run_channels(run, config, on_retry)
    return await _run_channels(run, config, on_retry)


async def _run_channels(
    run: Callable[[SessionConfig], Awaitable[T]],
    config: SessionConfig,
    on_retry: Callable[[str | None, str | None], None] | None,
) -> T:
    requested = config.channel
    channels = _fallback_channels(requested)
    if requested in _resolved_channels:
        channels = channels[channels.index(_resolved_channels[requested]) :]
    for index, channel in enumerate(channels):
        selected = (
            config if channel == requested else config.model_copy(update={"channel": channel})
        )
        try:
            result = await run(selected)
        except Exception as exc:
            if is_sandbox_failure(exc):
                raise
            if not is_launch_failure(exc):
                # The browser started; the page or navigation failed.
                _resolved_channels[requested] = channel
                raise
            if index + 1 == len(channels):
                raise
            if on_retry is not None:
                on_retry(channel, channels[index + 1])
            continue
        _resolved_channels[requested] = channel
        return result
    raise RuntimeError("no browser channel to launch")


class WebSkrapSession:
    """One Chromium and the helpers that drive pages in it.

    Created by :meth:`WebSkrapClient.session`, not directly. Every page of a
    session shares its cookies and storage. The session owns its browser, so
    :meth:`close` is what releases it; ``async with`` does it for you. Every
    method raises :class:`WebSkrapError` once the session is closed.

    Attributes:
        name: Session name, unique within the owning client.
        browser: The :class:`~webskrap.cdp.CDPBrowser` behind the session.
        config: The :class:`~webskrap.models.SessionConfig` it was built from.
        profile: The :class:`~webskrap.models.BrowserProfile` applied to it.
    """

    def __init__(
        self,
        *,
        name: str,
        browser: CDPBrowser,
        config: SessionConfig,
        profile: BrowserProfile,
        display: VirtualDisplay | None = None,
    ) -> None:
        """Adopt a launched browser; :meth:`WebSkrapClient.session` calls this.

        The session takes ownership: closing it closes the browser (and its
        temporary profile) and the virtual display it runs on.
        """
        self.name = name
        self.browser = browser
        self.config = config
        self.profile = profile
        self._display = display
        self._closed = False

    async def __aenter__(self) -> WebSkrapSession:
        """Enter the session unchanged; its browser is already running."""
        return self

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        """Close the session, so a failing block still releases the browser."""
        await self.close()

    async def new_page(self) -> CDPPage:
        """Open a page with the session's profile and policies applied.

        Close it with :meth:`close_page` when done; the browser closes any page
        left open when the session closes.
        """
        self._ensure_open()
        page = await self.browser.new_page()
        try:
            await cdp_setup.prepare_page(page, self.config, self.profile)
        except BaseException:
            with CancelScope(shield=True):
                await self.close_page(page)
            raise
        return page

    async def close_page(self, page: CDPPage) -> None:
        """Close a page from :meth:`new_page`. Idempotent."""
        with suppress(WebSkrapError):
            await self.browser.connection.send(
                "Target.closeTarget", {"targetId": page.target_id}, timeout_s=5
            )

    async def fetch(
        self,
        url: str,
        *,
        wait_until: WaitUntil = "domcontentloaded",
        screenshot: bool | str | Path = False,
        timeout_ms: float | None = None,
        text_only: bool = False,
        include_links: bool = False,
        max_links: int = 50,
        decline_cookies: bool | None = None,
        ready_selector: str | None = None,
    ) -> FetchResult:
        """Open ``url`` in a new page, read it, and close the page.

        The page is always closed, so nothing survives the call except the
        returned data and any cookies the session picked up. When
        ``SessionConfig.decline_cookies`` is set, a consent notice is dismissed
        after navigation and before the text is read.

        Args:
            url: The URL to load.
            wait_until: Load state to wait for.
            screenshot: True for a generated filename, or a path to write a
                full-page PNG to. The path is used as given, so pass a
                destination you control.
            timeout_ms: Navigation timeout; defaults to the config's.
            text_only: Return visible body text instead of page HTML.
            include_links: Also collect the page's outbound links. Off by
                default because a link-heavy page costs more to return than the
                caller may want.
            max_links: How many links to keep. ``FetchResult.links_total``
                reports how many there were before the cap.
            decline_cookies: Per-call consent override; None uses the config.
            ready_selector: Optional CSS selector or snapshot ref that must
                become visible after consent dismissal. Uses the navigation
                timeout separately.

        Returns:
            A :class:`~webskrap.models.FetchResult`. ``ok`` reflects the HTTP
            status, so a 404 returns normally with ``ok=False``.

        Raises:
            WebSkrapError: If the session is closed, the readiness selector is
                blank or invalid (``usage``), a known challenge is detected
                (``blocked``), the network fails (``navigation``), or a wait
                runs out (``timeout``).
        """
        self._ensure_open()
        url = validate_url(url)
        if ready_selector is not None and not ready_selector.strip():
            raise WebSkrapError("ready_selector cannot be blank", code=ErrorCode.USAGE)
        started = time.perf_counter()
        timeout_s = (timeout_ms or self.config.navigation_timeout_ms) / 1000
        page = await self.new_page()
        documents = record_documents(page)
        try:
            deadline = time.monotonic() + timeout_s
            response = await page.goto(url, wait_until=wait_until, timeout_s=timeout_s)
            response = await settle(
                page, documents, response, wait_until=wait_until, deadline=deadline
            )
            headers = response_headers(response)
            status = response_status(response)
            await raise_for_challenge(page, headers, status)
            declined = await self._decline_after_navigation(
                page, wait_until, decline_cookies=decline_cookies
            )
            if ready_selector is not None:
                await _wait_visible(
                    page, ready_selector, timeout_s=timeout_s, headers=headers, status=status
                )
            await raise_for_challenge(page, headers, status)
            state = await page.evaluate(_STATE_SCRIPT)
            text = state["text"] if text_only else await page.evaluate(_CONTENT_SCRIPT)
            links, links_total = await _collect_links(
                page,
                max_links if include_links and self.config.java_script_enabled else None,
            )
            screenshot_path = await _maybe_screenshot(page, screenshot)
            cookies = await self.cookies()
            return FetchResult(
                url=url,
                final_url=state["url"],
                status=status,
                ok=status is not None and 200 <= status < 400,
                headers=headers,
                text=text,
                title=state["title"],
                cookies=cookies,
                timings={"elapsed_ms": (time.perf_counter() - started) * 1000},
                screenshot_path=screenshot_path,
                cookie_notice_declined=declined,
                links=links,
                links_total=links_total,
            )
        finally:
            documents.stop()
            with CancelScope(shield=True):
                await self.close_page(page)

    async def search(
        self,
        query: str,
        *,
        engine: SearchEngine = SearchEngine.BING,
        max_results: int = 10,
        timeout_ms: float | None = None,
    ) -> SearchResult:
        """Load ``engine``'s results page for ``query`` and return its hits.

        The page is loaded like a :meth:`fetch` -- same session, so its proxy,
        consent dismissal and persistent profile apply unchanged -- and parsed
        by :mod:`webskrap.search`. Nothing is retried: an engine that serves a
        challenge page is reported, not argued with.

        Args:
            query: Words to search for; surrounding whitespace is ignored.
            engine: Which engine's results page to load.
            max_results: How many hits to keep. ``SearchResult.hits_total``
                reports how many the page held before the cap.
            timeout_ms: Navigation timeout; defaults to the config's.

        Returns:
            A :class:`~webskrap.models.SearchResult`.

        Raises:
            WebSkrapError: If the session is closed, the query is blank
                (``usage``), or the engine answered with a bot challenge
                (``blocked``).
        """
        self._ensure_open()
        url = search_url(engine, query)
        started = time.perf_counter()
        timeout_s = (timeout_ms or self.config.navigation_timeout_ms) / 1000
        page = await self.new_page()
        documents = record_documents(page)
        try:
            deadline = time.monotonic() + timeout_s
            response = await page.goto(url, wait_until="domcontentloaded", timeout_s=timeout_s)
            # Bing answers a cookie-less session with a head-only interstitial
            # whose script navigates to the real page, and reading at
            # DOMContentLoaded can catch it. Every results, no-results or
            # challenge page renders something in the body, so wait for that;
            # the wait survives the interstitial's navigation.
            await _wait_attached(page, "body > *", timeout_s=timeout_s)
            # The same provider check as fetch, before the engine's own markers.
            response = await settle(
                page, documents, response, wait_until="domcontentloaded", deadline=deadline
            )
            await raise_for_challenge(page, response_headers(response), response_status(response))
            declined = await self._decline_after_navigation(page, "domcontentloaded")
            html = await page.evaluate(_CONTENT_SCRIPT)
            final_url = (await page.evaluate(_STATE_SCRIPT))["url"]
        finally:
            documents.stop()
            with CancelScope(shield=True):
                await self.close_page(page)
        hits = parse_results(engine, html)
        status = response_status(response)
        return SearchResult(
            query=query,
            engine=engine,
            url=url,
            final_url=final_url,
            status=status,
            ok=status is not None and 200 <= status < 400,
            hits=hits[: max(0, max_results)],
            hits_total=len(hits),
            timings={"elapsed_ms": (time.perf_counter() - started) * 1000},
            cookie_notice_declined=declined,
        )

    async def cookies(self) -> list[dict[str, Any]]:
        """Return every cookie in the session, in Playwright's cookie shape."""
        self._ensure_open()
        result = await self.browser.connection.send("Storage.getCookies")
        return [
            {
                "name": cookie["name"],
                "value": cookie["value"],
                "domain": cookie["domain"],
                "path": cookie["path"],
                "expires": -1 if cookie.get("session") else cookie.get("expires", -1),
                "httpOnly": cookie.get("httpOnly", False),
                "secure": cookie.get("secure", False),
                "sameSite": cookie.get("sameSite", "Lax"),
            }
            for cookie in result.get("cookies", [])
        ]

    async def _decline_after_navigation(
        self, page: CDPPage, wait_until: WaitUntil, *, decline_cookies: bool | None = None
    ) -> str | None:
        """Dismiss a consent notice once ``page`` has navigated, if configured."""
        enabled = self.config.decline_cookies if decline_cookies is None else decline_cookies
        if not enabled:
            return None
        budget = self.config.decline_cookies_timeout_ms
        if wait_until == "networkidle":
            # The navigation already waited out the CMP script.
            budget = min(budget, SETTLED_PAGE_TIMEOUT_MS)
        return await self.decline_cookies(page, timeout_ms=budget)

    async def decline_cookies(
        self, page: CDPPage, *, timeout_ms: float | None = None
    ) -> str | None:
        """Click a cookie consent notice's reject control on ``page``.

        Called automatically by :meth:`fetch` unless
        ``SessionConfig.decline_cookies`` is False. Call it directly for pages
        you drive yourself. Returns the strategy that clicked, or None.
        """
        self._ensure_open()
        if timeout_ms is None:
            timeout_ms = self.config.decline_cookies_timeout_ms
        return await _decline_cookies(page, timeout_ms=timeout_ms)

    async def human_click(
        self,
        page: CDPPage,
        selector: str,
        *,
        human: bool = True,
        click_count: int = 1,
        timeout: float | None = None,
    ) -> None:
        """Click ``selector`` (CSS or snapshot ref) along a human cursor path.

        The cursor moves on an eased Bezier curve with jitter and pauses, the
        button is held as long as a person holds it, and a covered element is
        refused. An empty box gets one scroll and measurement retry. The target
        is checked again after moving the cursor, before pressing the button.
        ``human=False`` clicks at the element's center at once.

        Args:
            page: Page from :meth:`new_page`.
            selector: CSS selector or snapshot ref (``e12``) of one element.
            human: Use the humanized path.
            click_count: 1 for a click, 2 for a double-click.
            timeout: Milliseconds to wait for the element; the page default
                when None.

        Raises:
            WebSkrapError: If the session is closed, the selector matches more
                than one element or the element is covered (``usage``), or it
                does not become visible and enabled in time (``timeout``).
        """
        self._ensure_open()
        if click_count not in (1, 2):
            raise WebSkrapError("click_count must be 1 or 2", code=ErrorCode.USAGE)
        with _PageTimeout(page, timeout):
            if human:
                action = "dblclick" if click_count == 2 else "click"
                await cdp_dom.element_action(page, action, selector, [])
            else:
                await cdp_dom.instant_click(page, selector, click_count=click_count)

    async def human_type(
        self, page: CDPPage, selector: str, text: str, *, timeout: float | None = None
    ) -> None:
        """Click into ``selector`` and type ``text`` with human keystroke timing.

        Keys are held 30 to 90 ms and spaced 40 to 160 ms, with an occasional
        longer pause. ``timeout`` is in milliseconds, as for :meth:`human_click`.

        Raises:
            WebSkrapError: If the session is closed or the field cannot be
                reached; see :meth:`human_click`.
        """
        self._ensure_open()
        with _PageTimeout(page, timeout):
            await cdp_dom.element_action(page, "type", selector, [text])

    async def close(self) -> None:
        """Close the browser, its temporary profile, and the virtual display.

        Idempotent; safe to call after a failed fetch.
        """
        if self._closed:
            return
        self._closed = True
        # MCP cancellation is level-triggered; cleanup must survive every checkpoint.
        with CancelScope(shield=True):
            try:
                await self.browser.close()
            finally:
                if self._display is not None:
                    await self._display.stop()
                    self._display = None

    def _ensure_open(self) -> None:
        if self._closed:
            msg = f"session '{self.name}' is closed"
            raise WebSkrapError(msg)


class _PageTimeout:
    """Temporarily set ``page.default_timeout_s`` from a millisecond value."""

    def __init__(self, page: CDPPage, timeout_ms: float | None) -> None:
        self.page = page
        self.timeout_ms = timeout_ms
        self.previous = page.default_timeout_s

    def __enter__(self) -> None:
        if self.timeout_ms is not None:
            self.page.default_timeout_s = self.timeout_ms / 1000

    def __exit__(self, *_: object) -> None:
        self.page.default_timeout_s = self.previous


class WebSkrapClient:
    """Hands out sessions and closes them all on exit.

    Use it as an async context manager; :meth:`close` shuts down every session
    it created, so a leaked browser process cannot outlive the block.

    Args:
        default_config: Config used when a call passes none.
        profiles: Extra named profiles, resolvable by name alongside the
            bundled ones.

    Attributes:
        default_config: The fallback :class:`~webskrap.models.SessionConfig`.
        profiles: Caller-supplied profiles by name.
    """

    def __init__(
        self,
        *,
        default_config: SessionConfig | None = None,
        profiles: Mapping[str, BrowserProfile] | None = None,
    ) -> None:
        """Configure the client without starting anything.

        A browser launches on the first :meth:`fetch` or :meth:`session` call,
        so constructing a client is cheap and cannot fail just because no
        browser is installed yet.
        """
        self.default_config = default_config or SessionConfig()
        self.profiles = dict(profiles or {})
        self._close_lock = asyncio.Lock()
        self._closing = False
        self._generation = 0
        self._session_tasks: dict[str, asyncio.Task[WebSkrapSession]] = {}
        self._sessions: dict[str, WebSkrapSession] = {}
        self._ua_cache: dict[tuple[str, bool], str] = {}
        self._ua_lock = asyncio.Lock()

    async def __aenter__(self) -> WebSkrapClient:
        """Enter the client; browsers still start on first use, not here."""
        return self

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        """Close every session this client opened."""
        await self.close()

    async def close(self) -> None:
        """Close every session.

        Waits for sessions that are still starting so none escape shutdown. If
        several sessions fail to close, the first failure is raised after the
        rest have been cleaned up.
        """
        async with self._close_lock:
            self._closing = True
            self._generation += 1
            try:
                pending = await asyncio.gather(
                    *self._session_tasks.values(),
                    return_exceptions=True,
                )
                sessions = list(self._sessions.values())
                sessions.extend(
                    result for result in pending if not isinstance(result, BaseException)
                )
                unique_sessions = {id(session): session for session in sessions}
                results = await asyncio.gather(
                    *(session.close() for session in unique_sessions.values()),
                    return_exceptions=True,
                )
                self._sessions.clear()
                self._session_tasks.clear()
                self._ua_cache.clear()
            finally:
                self._closing = False
        if error := next((result for result in results if isinstance(result, BaseException)), None):
            raise error

    async def fetch(
        self,
        url: str,
        *,
        profile: str | BrowserProfile | None = None,
        config: SessionConfig | None = None,
        wait_until: WaitUntil = "domcontentloaded",
        screenshot: bool | str | Path = False,
        timeout_ms: float | None = None,
        text_only: bool = False,
        include_links: bool = False,
        max_links: int = 50,
        decline_cookies: bool | None = None,
        ready_selector: str | None = None,
    ) -> FetchResult:
        """Fetch one URL in a throwaway session.

        The session is created and closed around the fetch, so cookies do not
        carry over between calls. Use :meth:`session` when they should.

        Args:
            url: The URL to load.
            profile: Profile name, :class:`~webskrap.models.BrowserProfile`, or
                None for the default.
            config: Session config; defaults to ``default_config``.
            wait_until: Load state to wait for.
            screenshot: True or a path to write a full-page PNG.
            timeout_ms: Navigation timeout override.
            text_only: Return visible body text instead of page HTML.
            include_links: Also collect the page's outbound links.
            max_links: How many links to keep.
            decline_cookies: Per-call consent override; None uses the config.
            ready_selector: Optional selector to wait for; see
                :meth:`WebSkrapSession.fetch`.

        Returns:
            A :class:`~webskrap.models.FetchResult`.

        Raises:
            WebSkrapError: If the client is closing or the browser cannot start.
        """
        name = f"_single_{uuid4().hex}"
        session = await self.session(name, config=config, profile=profile)
        try:
            return await session.fetch(
                url,
                wait_until=wait_until,
                screenshot=screenshot,
                timeout_ms=timeout_ms,
                text_only=text_only,
                include_links=include_links,
                max_links=max_links,
                decline_cookies=decline_cookies,
                ready_selector=ready_selector,
            )
        finally:
            await session.close()
            self._sessions.pop(name, None)

    async def search(
        self,
        query: str,
        *,
        engine: SearchEngine = SearchEngine.BING,
        max_results: int = 10,
        profile: str | BrowserProfile | None = None,
        config: SessionConfig | None = None,
        timeout_ms: float | None = None,
    ) -> SearchResult:
        """Search in a throwaway session; see :meth:`WebSkrapSession.search`.

        Args:
            query: Words to search for.
            engine: Which engine's results page to load.
            max_results: How many hits to keep.
            profile: Profile name, :class:`~webskrap.models.BrowserProfile`, or
                None for the default.
            config: Session config; defaults to ``default_config``.
            timeout_ms: Navigation timeout override.

        Returns:
            A :class:`~webskrap.models.SearchResult`.

        Raises:
            WebSkrapError: If the client is closing, the browser cannot start,
                the query is blank, or the engine served a bot challenge.
        """
        name = f"_single_{uuid4().hex}"
        session = await self.session(name, config=config, profile=profile)
        try:
            return await session.search(
                query, engine=engine, max_results=max_results, timeout_ms=timeout_ms
            )
        finally:
            await session.close()
            self._sessions.pop(name, None)

    async def session(
        self,
        name: str,
        *,
        config: SessionConfig | None = None,
        profile: str | BrowserProfile | None = None,
    ) -> WebSkrapSession:
        """Return the named session, creating it on first use.

        Repeat calls with the same name return the same live session, so its
        cookies and storage persist across fetches. ``config`` and ``profile``
        only apply to the call that creates it. Concurrent callers racing on
        one name get the same session, not two browsers.

        Args:
            name: Session name, unique within this client.
            config: Session config; defaults to ``default_config``.
            profile: Profile name, :class:`~webskrap.models.BrowserProfile`, or
                None for the default.

        Returns:
            The live :class:`WebSkrapSession`.

        Raises:
            WebSkrapError: If the client is closing, closes mid-start, or the
                browser cannot launch.
        """
        if self._closing:
            msg = "client is closing"
            raise WebSkrapError(msg)

        existing = self._sessions.get(name)
        if existing is not None and not existing._closed:
            return existing
        self._sessions.pop(name, None)

        generation = self._generation
        resolved_config = config or self.default_config
        task = self._session_tasks.get(name)
        if task is None:
            resolved_profile = self._resolve_profile(profile)
            task = asyncio.create_task(
                self._create_session(name, resolved_config, resolved_profile)
            )
            self._session_tasks[name] = task
            task.add_done_callback(
                lambda completed: self._session_finished(name, generation, completed)
            )
        # One cancelled waiter must not cancel another caller's shared launch.
        session = await asyncio.shield(task)
        if generation != self._generation:
            msg = "client closed while the session was starting"
            raise WebSkrapError(msg)
        self._sessions[name] = session
        return session

    def _session_finished(
        self, name: str, generation: int, task: asyncio.Task[WebSkrapSession]
    ) -> None:
        if self._session_tasks.get(name) is task:
            self._session_tasks.pop(name)
        if task.cancelled() or task.exception() is not None:
            return
        # Keep ownership even when every waiter was cancelled during launch.
        if generation == self._generation and not self._closing:
            self._sessions[name] = task.result()

    def _resolve_profile(self, profile: str | BrowserProfile | None) -> BrowserProfile:
        if isinstance(profile, BrowserProfile):
            return profile
        if profile in self.profiles:
            return self.profiles[profile].model_copy(deep=True)
        return get_profile(profile)

    async def close_session(self, name: str) -> bool:
        """Close and forget a named session, returning False if absent.

        Call after its fetches finish. Closing an active session interrupts its
        pages; this method does not schedule or wait for page operations.
        """
        task = self._session_tasks.get(name)
        if task is not None:
            try:
                session = await asyncio.shield(task)
            except Exception:
                # Failed launches leave no browser to release.
                return False
        else:
            session = self._sessions.get(name)
        if session is None:
            return False
        await session.close()
        if self._sessions.get(name) is session:
            self._sessions.pop(name, None)
        return True

    async def _create_session(
        self,
        name: str,
        config: SessionConfig,
        profile: BrowserProfile,
    ) -> WebSkrapSession:
        try:
            args = config.browser_args(profile)
            env = config.browser_env(profile)
        except ValueError as exc:
            raise WebSkrapError(str(exc), code=ErrorCode.USAGE) from exc
        # A persistent profile already carries its own state.
        state = None
        if config.storage_state is not None and config.user_data_dir is None:
            state = cdp_setup.load_storage_state(config.storage_state)
        executable = find_executable(config.channel)
        if (
            config.mask_headless_user_agent
            and config.launch_headless
            and not any(a.startswith("--user-agent") for a in args)
        ):
            clean_ua = await self._headless_clean_user_agent(executable, config)
            if clean_ua:
                # Applied via the launch flag only. It covers the page, every
                # worker (including SharedWorker, a separate process) and
                # request headers process-wide; a per-page CDP override does
                # not reach workers.
                args.append(f"--user-agent={clean_ua}")

        display = None
        browser = None
        try:
            if config.gpu is GpuBackend.MESA:
                require_lavapipe()
            if config.uses_virtual_display():
                screen = config.virtual_screen()
                display = await VirtualDisplay.start(screen.width, screen.height)
                env = {**(env or os.environ), **display.env}
            if config.user_data_dir is not None:
                config.user_data_dir.mkdir(parents=True, exist_ok=True)
            browser = await launch(
                executable,
                args=args,
                headless=config.launch_headless,
                user_data_dir=config.user_data_dir,
                env=env,
                timeout_s=config.timeout_ms / 1000,
            )
            browser.connection.default_timeout_s = config.default_timeout_ms / 1000
            await cdp_setup.prepare_browser(browser, config, profile)
            if state is not None:
                await cdp_setup.apply_storage_state(browser, state)
        except BaseException:
            with CancelScope(shield=True):
                if browser is not None:
                    with suppress(Exception):
                        await browser.close()
                if display is not None:
                    await display.stop()
            raise
        return WebSkrapSession(
            name=name, browser=browser, config=config, profile=profile, display=display
        )

    async def _headless_clean_user_agent(
        self, executable: Path, config: SessionConfig
    ) -> str | None:
        # Concurrent sessions share one successful probe per executable.
        # Failures are not cached, so installing a browser can recover in place.
        key = (str(executable), config.chromium_sandbox)
        async with self._ua_lock:
            if key in self._ua_cache:
                return self._ua_cache[key]
            ua = await _probe_headless_user_agent(executable, config)
            if ua is not None:
                self._ua_cache[key] = ua
            return ua


async def _probe_headless_user_agent(executable: Path, config: SessionConfig) -> str | None:
    """Read the real headless UA and rewrite its ``HeadlessChrome`` token.

    Returns None if the probe fails or the UA has no headless tell, leaving
    the native UA untouched.
    """
    args = [] if config.chromium_sandbox else ["--no-sandbox"]
    try:
        browser = await launch(executable, args=args, headless=True)
    except Exception:  # noqa: BLE001 - probe is best-effort
        return None
    try:
        ua = (await browser.connection.send("Browser.getVersion")).get("userAgent")
    except Exception:  # noqa: BLE001 - probe is best-effort
        return None
    finally:
        await browser.close()
    if not isinstance(ua, str) or "HeadlessChrome" not in ua:
        return None
    return ua.replace("HeadlessChrome", "Chrome")


#: Where the Vulkan loader looks for driver manifests on Linux.
VULKAN_ICD_DIRS = (
    Path("/usr/share/vulkan/icd.d"),
    Path("/usr/local/share/vulkan/icd.d"),
    Path("/etc/vulkan/icd.d"),
)


def lavapipe_available() -> bool:
    """True when Mesa's software Vulkan driver (lavapipe) is installed."""
    if not sys.platform.startswith("linux"):
        return False
    if os.environ.get("VK_DRIVER_FILES") or os.environ.get("VK_ICD_FILENAMES"):
        # An explicit driver list is the caller's choice; trust it.
        return True
    return any(
        any(directory.glob("lvp_icd*.json")) for directory in VULKAN_ICD_DIRS if directory.is_dir()
    )


def require_lavapipe() -> None:
    """Raise ``browser_launch`` unless Mesa's lavapipe driver is installed.

    Without the driver Chromium does not fall back: WebGL just disappears,
    which is not what a caller asking for Mesa wanted.
    """
    if not lavapipe_available():
        msg = (
            "gpu='mesa' needs Linux with Mesa's lavapipe Vulkan driver. "
            "Install it (Debian/Ubuntu: apt install mesa-vulkan-drivers)"
        )
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)


async def _wait_visible(
    page: CDPPage, target: str, *, timeout_s: float, headers: dict[str, str], status: int | None
) -> None:
    """Wait for ``target`` (CSS or ref) to be visible; check for a challenge on timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        with suppress(CDPError):  # a navigation destroyed the world mid-call
            if await cdp_dom.element_state(page, target) == "visible":
                return
        if time.monotonic() >= deadline:
            # A challenge can replace the page while readiness is pending.
            await raise_for_challenge(page, headers, status)
            msg = f"timed out after {timeout_s * 1000:.0f}ms waiting for {target} to be visible"
            raise WebSkrapError(msg, code=ErrorCode.TIMEOUT)
        await asyncio.sleep(_POLL_S)


async def _wait_attached(page: CDPPage, selector: str, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        with suppress(CDPError):  # a navigation destroyed the world mid-call
            if await cdp_dom.element_state(page, selector) != "detached":
                return
        if time.monotonic() >= deadline:
            msg = f"timed out after {timeout_s * 1000:.0f}ms waiting for {selector}"
            raise WebSkrapError(msg, code=ErrorCode.TIMEOUT)
        await asyncio.sleep(_POLL_S)


async def _maybe_screenshot(page: CDPPage, screenshot: bool | str | Path) -> Path | None:
    if not screenshot:
        return None
    path = Path(f"webskrap-{uuid4().hex}.png") if screenshot is True else Path(screenshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = await page.screenshot(full_page=True)
    await asyncio.to_thread(path.write_bytes, image)
    return path


async def _collect_links(page: CDPPage, max_links: int | None) -> tuple[list[Link], int]:
    """Return ``page``'s outbound links and how many there were before the cap.

    ``max_links`` of None means the caller did not ask for links, or JavaScript
    is disabled for this session; both return an empty list and a zero total
    rather than failing the fetch.
    """
    if max_links is None:
        return [], 0
    collected = await page.evaluate(LINKS_SCRIPT, max_links)
    return [Link(**link) for link in collected["links"]], int(collected["total"])
