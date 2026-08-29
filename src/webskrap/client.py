"""Async scraping client: Playwright/Patchright sessions, fetches, and clicks.

:class:`WebSkrapClient` owns the browser driver and hands out
:class:`WebSkrapSession` objects; a session owns one browser context and the
pages opened from it. Both are async context managers, and both raise
:class:`WebSkrapError` for WebSkrap-level failures while letting Playwright's
own errors (timeouts, navigation failures) propagate unchanged.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from secrets import SystemRandom
from typing import Any
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, FloatRect, Page

from webskrap.consent import SETTLED_PAGE_TIMEOUT_MS
from webskrap.consent import decline_cookies as _decline_cookies
from webskrap.errors import RECOVERY_HINTS, ErrorCode, WebSkrapError
from webskrap.models import (
    BrowserProfile,
    FetchResult,
    Link,
    ResourcePolicy,
    SessionConfig,
    WaitUntil,
)
from webskrap.profiles import get_profile

# Cursor jitter below is pixel offsets and sleep durations, never a token,
# identifier, or security decision, so `random` would be adequate. It draws
# from system entropy anyway: a few dozen values per click cost nothing next to
# the millisecond sleeps between mouse moves, and it keeps the module free of
# predictable-RNG calls that a security scanner would have to be told to ignore.
uniform = SystemRandom().uniform

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
    channels: tuple[str | None, ...] = ("chrome", None),
) -> dict[str, object]:
    """Report the first Chromium channel that launches with ``driver``.

    Also reports the bundled Chromium binary the driver would use, since a
    caller diagnosing a failed launch otherwise has to guess where the browser
    was looked for.
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
            browser = await playwright.chromium.launch(channel=channel, headless=True)
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
        "hint": RECOVERY_HINTS[ErrorCode.BROWSER_LAUNCH],
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
    ) -> None:
        """Adopt an already-open context; :meth:`WebSkrapClient.session` calls this.

        The session takes ownership: closing it closes the context, the browser
        when one was passed, and any temporary profile directory.
        """
        self.name = name
        self.context = context
        self.config = config
        self.profile = profile
        self.browser = browser
        self._temp_user_data_dir = temp_user_data_dir
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
        started = time.perf_counter()
        page = await self.context.new_page()
        try:
            response = await page.goto(
                url,
                wait_until=wait_until,
                timeout=timeout_ms or self.config.navigation_timeout_ms,
            )
            declined = None
            if self.config.decline_cookies:
                budget = self.config.decline_cookies_timeout_ms
                if wait_until == "networkidle":
                    # The navigation already waited out the CMP script.
                    budget = min(budget, SETTLED_PAGE_TIMEOUT_MS)
                declined = await self.decline_cookies(page, timeout_ms=budget)
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

        locator = page.locator(selector)
        timeout = click_options.get("timeout")
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.scroll_into_view_if_needed(timeout=timeout)

        if click_options.get("strict") is True and await locator.count() != 1:
            msg = f"strict mode expected one element for selector: {selector}"
            raise WebSkrapError(msg, code=ErrorCode.USAGE)

        box = await locator.bounding_box(timeout=timeout)
        if box is None:
            msg = f"could not find a visible bounding box for selector: {selector}"
            raise WebSkrapError(msg, code=ErrorCode.USAGE)

        x, y = _human_click_point(box, click_options.get("position"))
        if click_options.get("trial"):
            return

        await page.wait_for_timeout(uniform(80, 220))

        start_x = x + uniform(-160, 160)
        start_y = y + uniform(-90, 90)
        await page.mouse.move(start_x, start_y, steps=1)
        end_x = x + uniform(-8, 8)
        end_y = y + uniform(-6, 6)
        distance = ((end_x - start_x) ** 2 + (end_y - start_y) ** 2) ** 0.5
        steps = max(12, min(48, int(distance / 6)))
        for px, py in _bezier_path((start_x, start_y), (end_x, end_y), steps):
            await page.mouse.move(px, py, steps=1)
            await page.wait_for_timeout(uniform(2, 9))
        await page.wait_for_timeout(uniform(40, 140))

        mouse_options = _mouse_click_options(click_options)
        modifiers = click_options.get("modifiers") or []
        for modifier in modifiers:
            await page.keyboard.down(modifier)
        try:
            await page.mouse.click(x, y, **mouse_options)
        finally:
            for modifier in reversed(modifiers):
                await page.keyboard.up(modifier)

    async def close(self) -> None:
        """Close the context, its browser, and any temporary profile directory.

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
                    shutil.rmtree(self._temp_user_data_dir, ignore_errors=True)
                    self._temp_user_data_dir = None
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
        launch_options = config.launch_options()

        if config.mask_headless_user_agent and config.headless and config.browser == "chromium":
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
        try:
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
            if temp_user_data_dir is not None:
                shutil.rmtree(temp_user_data_dir, ignore_errors=True)
            raise
        return WebSkrapSession(
            name=name,
            context=context,
            config=config,
            profile=profile,
            browser=browser,
            temp_user_data_dir=temp_user_data_dir,
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


def _human_click_point(
    box: FloatRect,
    position: Mapping[str, float] | None,
) -> tuple[float, float]:
    if position is not None:
        return box["x"] + position["x"], box["y"] + position["y"]

    jitter_x = min(box["width"] * 0.2, 6)
    jitter_y = min(box["height"] * 0.2, 6)
    return (
        box["x"] + box["width"] / 2 + uniform(-jitter_x, jitter_x),
        box["y"] + box["height"] / 2 + uniform(-jitter_y, jitter_y),
    )


def _bezier_path(
    start: tuple[float, float],
    end: tuple[float, float],
    steps: int,
) -> list[tuple[float, float]]:
    """Curved, eased cursor path from start to end.

    Mimics HumanCursor's trajectory: a cubic Bezier bent off the straight line
    by randomized control points, with smoothstep-eased spacing so velocity
    ramps up then slows near the target instead of moving in a straight,
    evenly-spaced line (the linear ``mouse.move(steps=n)`` robot tell).
    """
    x0, y0 = start
    x3, y3 = end
    dx, dy = x3 - x0, y3 - y0
    distance = max(1.0, (dx * dx + dy * dy) ** 0.5)
    nx, ny = -dy / distance, dx / distance  # unit normal to the straight line
    bend = distance * uniform(0.08, 0.22) * (1 if uniform(0, 1) < 0.5 else -1)
    cx1 = x0 + dx / 3 + nx * bend * uniform(0.6, 1.0)
    cy1 = y0 + dy / 3 + ny * bend * uniform(0.6, 1.0)
    cx2 = x0 + dx * 2 / 3 + nx * bend * uniform(0.6, 1.0)
    cy2 = y0 + dy * 2 / 3 + ny * bend * uniform(0.6, 1.0)

    points: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)  # smoothstep -> non-uniform speed
        mt = 1 - t
        bx = mt**3 * x0 + 3 * mt**2 * t * cx1 + 3 * mt * t**2 * cx2 + t**3 * x3
        by = mt**3 * y0 + 3 * mt**2 * t * cy1 + 3 * mt * t**2 * cy2 + t**3 * y3
        taper = mt  # jitter fades to zero at the target
        points.append((bx + uniform(-1.2, 1.2) * taper, by + uniform(-1.2, 1.2) * taper))
    points[-1] = (x3, y3)
    return points


def _mouse_click_options(click_options: dict[str, Any]) -> dict[str, Any]:
    mouse_options: dict[str, Any] = {}
    if "button" in click_options:
        mouse_options["button"] = click_options["button"]
    if "click_count" in click_options:
        mouse_options["click_count"] = click_options["click_count"]
    if "delay" in click_options:
        mouse_options["delay"] = click_options["delay"]
    return mouse_options


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
