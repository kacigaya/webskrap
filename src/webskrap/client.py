"""Async scraping client: Playwright/Patchright sessions, fetches, and clicks.

:class:`WebSkrapClient` owns the browser driver and hands out
:class:`WebSkrapSession` objects; a session owns one browser context and the
pages opened from it. Both are async context managers, and both raise
:class:`WebSkrapError` for WebSkrap-level failures while letting Playwright's
own errors (timeouts, navigation failures) propagate unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Page

from webskrap import human as humanize
from webskrap.consent import SETTLED_PAGE_TIMEOUT_MS
from webskrap.consent import decline_cookies as _decline_cookies
from webskrap.display import VirtualDisplay
from webskrap.errors import RECOVERY_HINTS, ErrorCode, WebSkrapError, is_sandbox_failure
from webskrap.models import (
    BrowserProfile,
    FetchResult,
    GpuBackend,
    Link,
    ResourcePolicy,
    SearchEngine,
    SearchResult,
    SessionConfig,
    WaitUntil,
)
from webskrap.profiles import get_profile
from webskrap.search import parse_results, search_url
from webskrap.urls import validate_url

logger = logging.getLogger(__name__)

# Collects every anchor's resolved absolute URL once, in document order, and
# returns the first ``max`` of them plus the unique total. Deduplicating in the
# page keeps a nav bar repeated in a footer from filling the whole budget, and
# `a.href` (not getAttribute) resolves relative paths against the final URL.
_LINKS_SCRIPT = """(max) => {
  const seen = new Set();
  const links = [];
  for (const anchor of document.querySelectorAll('a[href]')) {
    const href = anchor.href;
    if (!href || href.startsWith('javascript:') || seen.has(href)) continue;
    seen.add(href);
    links.push({
      href,
      text: (anchor.innerText || anchor.textContent || '')
        .replace(/\\s+/g, ' ')
        .trim()
        .slice(0, 120),
    });
  }
  return { links: links.slice(0, Math.max(0, max)), total: links.length };
}"""


def _async_playwright(driver: str):
    """Return the async_playwright factory for the chosen driver.

    ``patchright`` is a drop-in, API-compatible fork of Playwright that hides the
    CDP ``Runtime.enable`` leak used by CDP-aware bot detectors. The package ships
    with WebSkrap, but its browser still needs downloading (``webskrap install``).
    """
    if driver == "patchright":
        try:
            from patchright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            msg = "driver='patchright' requires patchright. Run: pip install webskrap"
            raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH) from exc
        return async_playwright()
    from playwright.async_api import async_playwright

    return async_playwright()


async def browser_doctor(
    driver: str = "patchright",
    channels: tuple[str | None, ...] = ("chrome", "chromium"),
    *,
    chromium_sandbox: bool = True,
) -> dict[str, object]:
    """Report the first Chromium channel that launches with ``driver``.

    Also reports the bundled Chromium binary the driver would use, since a
    caller diagnosing a failed launch otherwise has to guess where the browser
    was looked for. ``chromium_sandbox`` should match what fetches will use,
    or a host that cannot sandbox reports ready while every fetch fails.
    """
    failure: Exception | None = None
    executable_path: str | None = None
    for channel in channels:
        playwright = None
        browser = None
        launched = False
        try:
            playwright = await _async_playwright(driver).start()
            executable_path = playwright.chromium.executable_path
            browser = await playwright.chromium.launch(
                channel=channel, headless=True, chromium_sandbox=chromium_sandbox
            )
            launched = True
        except Exception as exc:  # noqa: BLE001 - report launch/import failures
            failure = exc
        finally:
            if browser is not None:
                with suppress(Exception):
                    await browser.close()
            if playwright is not None:
                with suppress(Exception):
                    await playwright.stop()
        if launched:
            channel_name = channel or "chromium"
            return {
                "ok": True,
                "message": f"{driver.title()} headless {channel_name} is ready.",
                "driver": driver,
                "channel": channel_name,
                "executable_path": executable_path,
            }
    return {
        "ok": False,
        "message": f"{driver.title()} Chromium did not launch: {failure}",
        "driver": driver,
        "channel": None,
        "executable_path": executable_path,
        "hint": RECOVERY_HINTS[
            ErrorCode.SANDBOX
            if failure is not None and is_sandbox_failure(failure)
            else ErrorCode.BROWSER_LAUNCH
        ],
    }


class WebSkrapSession:
    """One browser context plus the helpers that drive pages in it.

    Created by :meth:`WebSkrapClient.session`, not directly. The session owns
    its context (and, for non-persistent runs, the browser behind it), so
    :meth:`close` is what releases those; ``async with`` does it for you. Every
    method raises :class:`WebSkrapError` once the session is closed.

    Attributes:
        name: Session name, unique within the owning client.
        context: The underlying Playwright ``BrowserContext``.
        config: The :class:`~webskrap.models.SessionConfig` it was built from.
        profile: The :class:`~webskrap.models.BrowserProfile` applied to it.
        browser: The owning ``Browser``, or None for a persistent context.
    """

    def __init__(
        self,
        *,
        name: str,
        context: BrowserContext,
        config: SessionConfig,
        profile: BrowserProfile,
        browser: Browser | None = None,
        temp_user_data_dir: str | None = None,
        display: VirtualDisplay | None = None,
    ) -> None:
        """Adopt an already-open context; :meth:`WebSkrapClient.session` calls this.

        The session takes ownership: closing it closes the context, the browser
        when one was passed, any temporary profile directory, and the virtual
        display the browser runs on.
        """
        self.name = name
        self.context = context
        self.config = config
        self.profile = profile
        self.browser = browser
        self._temp_user_data_dir = temp_user_data_dir
        self._display = display
        self._closed = False

    async def __aenter__(self) -> WebSkrapSession:
        """Enter the session unchanged; its context is already open."""
        return self

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        """Close the session, so a failing block still releases the browser."""
        await self.close()

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
    ) -> FetchResult:
        """Open ``url`` in a new page, read it, and close the page.

        The page is always closed, so nothing survives the call except the
        returned data and any cookies the context picked up. When
        ``SessionConfig.decline_cookies`` is set, a consent notice is dismissed
        after navigation and before the text is read.

        Args:
            url: The URL to load.
            wait_until: Playwright load state to wait for.
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

        Returns:
            A :class:`~webskrap.models.FetchResult`. ``ok`` reflects the HTTP
            status, so a 404 returns normally with ``ok=False``.

        Raises:
            WebSkrapError: If the session is closed.
        """
        self._ensure_open()
        url = validate_url(url)
        started = time.perf_counter()
        page = await self.context.new_page()
        try:
            response = await page.goto(
                url,
                wait_until=wait_until,
                timeout=timeout_ms or self.config.navigation_timeout_ms,
            )
            declined = await self._decline_after_navigation(page, wait_until)
            title = await page.title()
            text = await page.locator("body").inner_text() if text_only else await page.content()
            links, links_total = await _collect_links(
                page,
                max_links if include_links and self.config.java_script_enabled else None,
            )
            screenshot_path = await _maybe_screenshot(page, screenshot)
            cookies = [dict(cookie) for cookie in await self.context.cookies()]
            elapsed_ms = (time.perf_counter() - started) * 1000
            status = response.status if response else None
            headers = dict(response.headers) if response else {}
            return FetchResult(
                url=url,
                final_url=page.url,
                status=status,
                ok=status is not None and 200 <= status < 400,
                headers=headers,
                text=text,
                title=title,
                cookies=cookies,
                timings={"elapsed_ms": elapsed_ms},
                screenshot_path=screenshot_path,
                cookie_notice_declined=declined,
                links=links,
                links_total=links_total,
            )
        finally:
            await page.close()

    async def search(
        self,
        query: str,
        *,
        engine: SearchEngine = SearchEngine.BING,
        max_results: int = 10,
        timeout_ms: float | None = None,
    ) -> SearchResult:
        """Load ``engine``'s results page for ``query`` and return its hits.

        The page is loaded like a :meth:`fetch` -- same context, so the
        session's proxy, consent dismissal and persistent profile apply
        unchanged -- and parsed by :mod:`webskrap.search`. Nothing is retried:
        an engine that serves a challenge page is reported, not argued with.

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
        timeout = timeout_ms or self.config.navigation_timeout_ms
        page = await self.context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            # Bing answers a cookie-less session with a head-only interstitial
            # whose script navigates to the real page, and reading at
            # DOMContentLoaded can catch it. Every results, no-results or
            # challenge page renders something in the body, so wait for that;
            # the wait survives the interstitial's navigation.
            await page.wait_for_selector("body > *", state="attached", timeout=timeout)
            declined = await self._decline_after_navigation(page, "domcontentloaded")
            html = await page.content()
            final_url = page.url
        finally:
            await page.close()
        hits = parse_results(engine, html)
        elapsed_ms = (time.perf_counter() - started) * 1000
        status = response.status if response else None
        return SearchResult(
            query=query,
            engine=engine,
            url=url,
            final_url=final_url,
            status=status,
            ok=status is not None and 200 <= status < 400,
            hits=hits[: max(0, max_results)],
            hits_total=len(hits),
            timings={"elapsed_ms": elapsed_ms},
            cookie_notice_declined=declined,
        )

    async def _decline_after_navigation(self, page: Page, wait_until: WaitUntil) -> str | None:
        """Dismiss a consent notice once ``page`` has navigated, if configured."""
        if not self.config.decline_cookies:
            return None
        budget = self.config.decline_cookies_timeout_ms
        if wait_until == "networkidle":
            # The navigation already waited out the CMP script.
            budget = min(budget, SETTLED_PAGE_TIMEOUT_MS)
        return await self.decline_cookies(page, timeout_ms=budget)

    async def decline_cookies(self, page: Page, *, timeout_ms: float | None = None) -> str | None:
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
        page: Page,
        selector: str,
        *,
        human: bool = True,
        **click_options: Any,
    ) -> None:
        """Click ``selector`` along a curved, variable-speed cursor path.

        Playwright's own click teleports the cursor and moves in evenly spaced
        steps, which behavioral detectors read as automation. This drives the
        real mouse along an eased Bezier curve with jitter and pauses instead.
        It is slower than ``page.click`` by design; use ``human=False`` to fall
        straight through to Playwright when the timing does not matter.

        Args:
            page: Page to click on; must belong to this session's context.
            selector: Playwright selector for the target element.
            human: Use the humanized path. False delegates to ``page.click``.
            **click_options: Playwright click options. ``position``, ``timeout``,
                ``strict``, ``trial``, ``modifiers``, ``button``, ``click_count``
                and ``delay`` are honored; other options apply only when
                ``human=False``.

        Raises:
            WebSkrapError: If the session is closed, ``strict`` was requested
                and the selector is ambiguous, or the element has no visible
                bounding box.
        """
        self._ensure_open()
        if not human:
            await page.click(selector, **click_options)
            return
        await humanize.click(page, page.locator(selector), description=selector, **click_options)

    async def human_type(self, page: Page, selector: str, text: str, **options: Any) -> None:
        """Click into ``selector`` and type ``text`` with human keystroke timing.

        The counterpart of :meth:`human_click` for keyboard input; see
        :func:`webskrap.human.type_text`. ``timeout`` is honored.

        Raises:
            WebSkrapError: If the session is closed or the field has no
                visible bounding box.
        """
        self._ensure_open()
        await humanize.type_text(
            page,
            page.locator(selector),
            text,
            description=selector,
            timeout=options.get("timeout"),
        )

    async def close(self) -> None:
        """Close the context, its browser, temp profile and virtual display.

        Idempotent; safe to call after a failed fetch.
        """
        if self._closed:
            return
        try:
            await self.context.close()
        finally:
            try:
                if self.browser is not None:
                    await self.browser.close()
            finally:
                if self._temp_user_data_dir is not None:
                    try:
                        shutil.rmtree(self._temp_user_data_dir, ignore_errors=False)
                    except OSError:
                        logger.debug("could not remove temp profile %s", self._temp_user_data_dir)
                    self._temp_user_data_dir = None
                if self._display is not None:
                    await self._display.stop()
                    self._display = None
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            msg = f"session '{self.name}' is closed"
            raise WebSkrapError(msg)


class WebSkrapClient:
    """Owns a browser driver and the sessions started from it.

    Use it as an async context manager; :meth:`close` shuts down every session
    it created and then the driver, so a leaked browser process cannot outlive
    the block. One client speaks to one driver: mixing ``playwright`` and
    ``patchright`` sessions requires two clients.

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

        The browser driver launches lazily on the first :meth:`fetch` or
        :meth:`session` call, so constructing a client is cheap and cannot fail
        just because no browser is installed yet.
        """
        self.default_config = default_config or SessionConfig()
        self.profiles = dict(profiles or {})
        self._playwright: Any | None = None
        self._driver: str | None = None
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closing = False
        self._generation = 0
        self._session_tasks: dict[str, asyncio.Task[WebSkrapSession]] = {}
        self._sessions: dict[str, WebSkrapSession] = {}

    async def __aenter__(self) -> WebSkrapClient:
        """Enter the client; the driver still starts on first use, not here."""
        return self

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        """Close every session this client opened, then stop the driver."""
        await self.close()

    async def start(
        self,
        driver: str | None = None,
        *,
        _generation: int | None = None,
    ) -> None:
        """Start the browser driver if it is not running yet.

        Called for you by :meth:`session` and :meth:`fetch`. Concurrent callers
        share one driver, and starting twice is a no-op.

        Args:
            driver: ``playwright`` or ``patchright``; defaults to the config's.
            _generation: Internal close-race guard.

        Raises:
            WebSkrapError: If the client is closing, or a different driver is
                already running.
        """
        selected_driver = driver or self.default_config.driver
        async with self._start_lock:
            if self._closing or (_generation is not None and _generation != self._generation):
                msg = "client is closing"
                raise WebSkrapError(msg)
            if self._playwright is not None:
                if selected_driver != self._driver:
                    msg = (
                        f"client already started with driver='{self._driver}'; "
                        f"cannot use driver='{selected_driver}'"
                    )
                    raise WebSkrapError(msg)
                return
            manager = _async_playwright(selected_driver)
            playwright = await manager.start()
            if self._closing or (_generation is not None and _generation != self._generation):
                with suppress(Exception):
                    await playwright.stop()
                msg = "client closed while the browser driver was starting"
                raise WebSkrapError(msg)
            self._playwright = playwright
            self._driver = selected_driver

    async def close(self) -> None:
        """Close every session, then stop the driver.

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
                async with self._start_lock:
                    try:
                        if self._playwright is not None:
                            await self._playwright.stop()
                    finally:
                        self._playwright = None
                        self._driver = None
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
    ) -> FetchResult:
        """Fetch one URL in a throwaway session.

        The session is created and closed around the fetch, so cookies do not
        carry over between calls. Use :meth:`session` when they should.

        Args:
            url: The URL to load.
            profile: Profile name, :class:`~webskrap.models.BrowserProfile`, or
                None for the default.
            config: Session config; defaults to ``default_config``.
            wait_until: Playwright load state to wait for.
            screenshot: True or a path to write a full-page PNG.
            timeout_ms: Navigation timeout override.
            text_only: Return visible body text instead of page HTML.
            include_links: Also collect the page's outbound links.
            max_links: How many links to keep.

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
        await self.start(resolved_config.driver, _generation=generation)
        task = self._session_tasks.get(name)
        owns_task = task is None
        if task is None:
            resolved_profile = self._resolve_profile(profile)
            task = asyncio.create_task(
                self._create_session(name, resolved_config, resolved_profile)
            )
            self._session_tasks[name] = task
        try:
            session = await task
            if generation != self._generation:
                msg = "client closed while the session was starting"
                raise WebSkrapError(msg)
            self._sessions[name] = session
            return session
        finally:
            if owns_task and self._session_tasks.get(name) is task:
                self._session_tasks.pop(name)

    def _resolve_profile(self, profile: str | BrowserProfile | None) -> BrowserProfile:
        if isinstance(profile, BrowserProfile):
            return profile
        if profile in self.profiles:
            return self.profiles[profile].model_copy(deep=True)
        return get_profile(profile)

    async def _create_session(
        self,
        name: str,
        config: SessionConfig,
        profile: BrowserProfile,
    ) -> WebSkrapSession:
        if self._playwright is None:
            msg = "client is not started"
            raise WebSkrapError(msg)

        browser_type = getattr(self._playwright, config.browser)
        context_options = config.context_options(profile)
        try:
            launch_options = config.launch_options(profile)
        except ValueError as exc:
            raise WebSkrapError(str(exc), code=ErrorCode.USAGE) from exc

        if (
            config.mask_headless_user_agent
            and config.headless
            and not config.virtual_display
            and config.browser == "chromium"
        ):
            clean_ua = await self._headless_clean_user_agent(browser_type, config)
            if clean_ua:
                # Apply the clean UA via the launch flag only. It covers the
                # page, every worker (including SharedWorker, a separate process)
                # and request headers process-wide. A per-context user_agent
                # override is intentionally avoided: it makes patchright inject a
                # CDP UA override into every frame/worker, which stalls
                # reCAPTCHA's worker init under some event loops.
                args = list(launch_options.get("args", []))
                if not any(a.startswith("--user-agent") for a in args):
                    args.append(f"--user-agent={clean_ua}")
                launch_options["args"] = args

        # patchright's stealth is only fully effective in a persistent context, so
        # fall back to a throwaway temp profile when the caller did not supply one.
        temp_user_data_dir: str | None = None
        user_data_dir = config.user_data_dir
        if user_data_dir is None and config.driver == "patchright":
            temp_user_data_dir = tempfile.mkdtemp(prefix="webskrap-patchright-")
            user_data_dir = Path(temp_user_data_dir)

        browser = None
        context = None
        display = None
        try:
            if config.gpu is GpuBackend.MESA and config.browser == "chromium":
                _require_lavapipe()
            if config.uses_virtual_display():
                screen = config.virtual_screen()
                display = await VirtualDisplay.start(screen.width, screen.height)
                # Playwright replaces the browser environment when env is set.
                launch_options["env"] = {**launch_options.get("env", os.environ), **display.env}
            if user_data_dir is not None:
                user_data_dir.mkdir(parents=True, exist_ok=True)
                context = await browser_type.launch_persistent_context(
                    str(user_data_dir),
                    **launch_options,
                    **context_options,
                )
            else:
                browser = await browser_type.launch(**launch_options)
                context = await browser.new_context(**context_options)

            context.set_default_timeout(config.default_timeout_ms)
            context.set_default_navigation_timeout(config.navigation_timeout_ms)

            if config.resource_policy != ResourcePolicy.ALL:
                await context.route("**/*", _resource_route_handler(config.resource_policy))
        except BaseException:
            if context is not None:
                with suppress(Exception):
                    await context.close()
            if browser is not None:
                with suppress(Exception):
                    await browser.close()
            if display is not None:
                await display.stop()
            if temp_user_data_dir is not None:
                try:
                    await asyncio.to_thread(shutil.rmtree, temp_user_data_dir)
                except OSError:
                    logger.debug("could not remove temp profile %s", temp_user_data_dir)
            raise
        return WebSkrapSession(
            name=name,
            context=context,
            config=config,
            profile=profile,
            browser=browser,
            temp_user_data_dir=temp_user_data_dir,
            display=display,
        )

    async def _headless_clean_user_agent(
        self, browser_type: Any, config: SessionConfig
    ) -> str | None:
        # Probe the real headless UA in a throwaway browser, then rewrite the
        # "HeadlessChrome" token to "Chrome". Returns None if the probe fails or
        # the UA has no headless tell, leaving the native UA untouched.
        launch_options = config.launch_options()
        launch_options.pop("args", None)
        try:
            browser = await browser_type.launch(**launch_options)
        except Exception:  # noqa: BLE001 - probe is best-effort
            return None
        try:
            page = await browser.new_page()
            ua = await page.evaluate("() => navigator.userAgent")
        except Exception:  # noqa: BLE001 - probe is best-effort
            return None
        finally:
            await browser.close()
            await asyncio.sleep(2)
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


def _require_lavapipe() -> None:
    # Without the driver Chromium does not fall back: WebGL just disappears,
    # which is not what a caller asking for Mesa wanted.
    if not lavapipe_available():
        msg = (
            "gpu='mesa' needs Linux with Mesa's lavapipe Vulkan driver. "
            "Install it (Debian/Ubuntu: apt install mesa-vulkan-drivers)"
        )
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)


def _resource_route_handler(policy: ResourcePolicy):
    blocked = {
        ResourcePolicy.LITE: {"image", "font", "media"},
        ResourcePolicy.DOCUMENTS: {"image", "font", "media", "stylesheet"},
    }[policy]

    async def handle(route) -> None:
        if route.request.resource_type in blocked:
            await route.abort()
        else:
            await route.continue_()

    return handle


async def _maybe_screenshot(page: Page, screenshot: bool | str | Path) -> Path | None:
    if not screenshot:
        return None
    path = Path(f"webskrap-{uuid4().hex}.png") if screenshot is True else Path(screenshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(path), full_page=True)
    return path


async def _collect_links(page: Page, max_links: int | None) -> tuple[list[Link], int]:
    """Return ``page``'s outbound links and how many there were before the cap.

    ``max_links`` of None means the caller did not ask for links, or JavaScript
    is disabled for this session and the script could not run; both return an
    empty list and a zero total rather than failing the fetch.
    """
    if max_links is None:
        return [], 0
    collected = await page.evaluate(_LINKS_SCRIPT, max_links)
    return [Link(**link) for link in collected["links"]], int(collected["total"])
