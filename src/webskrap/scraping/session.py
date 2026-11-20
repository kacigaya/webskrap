"""Page ownership, fetching, and searching within a browser session."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from anyio import CancelScope

from webskrap.browser.consent import SETTLED_PAGE_TIMEOUT_MS
from webskrap.browser.consent import decline_cookies as _decline_cookies
from webskrap.browser.display import VirtualDisplay
from webskrap.challenges import (
    raise_for_challenge,
    record_documents,
    response_headers,
    response_status,
    settle,
)
from webskrap.config.models import (
    BrowserProfile,
    ResourcePolicy,
    SearchEngine,
    SessionConfig,
    WaitUntil,
)
from webskrap.driver import cdp_dom, cdp_setup
from webskrap.driver.cdp import CDPPage
from webskrap.driver.launch import CDPBrowser
from webskrap.driver.polling import poll
from webskrap.errors import (
    ErrorCode,
    WebSkrapError,
)
from webskrap.scraping.results import FetchResult, Link, SearchResult
from webskrap.scraping.search import parse_results, search_url
from webskrap.security.urls import validate_url

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
_EXTRACT_SCRIPT = """({ text_only, max_links }) => {
  const text = text_only
    ? (document.body ? document.body.innerText : '')
    : (CONTENT)();
  const collected = max_links === null ? { links: [], total: 0 } : (LINKS)(max_links);
  return {
    url: location.href,
    title: document.title,
    text,
    links: collected.links,
    links_total: collected.total,
  };
}""".replace("CONTENT", _CONTENT_SCRIPT).replace("LINKS", LINKS_SCRIPT)


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

    async def new_page(self, *, text_only: bool = False) -> CDPPage:
        """Open a page with the session's profile and policies applied.

        With the auto resource policy, text_only skips images, fonts and media.
        Close it with :meth:`close_page` when done; the browser closes any page
        left open when the session closes.
        """
        self._ensure_open()
        page = await self.browser.new_page()
        try:
            config = self.config
            if config.resource_policy is ResourcePolicy.AUTO:
                config = config.model_copy(
                    update={
                        "resource_policy": ResourcePolicy.LITE if text_only else ResourcePolicy.ALL
                    }
                )
            await cdp_setup.prepare_page(page, config, self.profile)
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
        solve_cloudflare: bool | None = None,
        solve_datadome: bool | None = None,
        solve_perimeterx: bool | None = None,
        solve_recaptcha: bool | None = None,
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
            text_only: Return visible body text instead of page HTML. The auto
                resource policy skips images, fonts and media unless a screenshot
                is requested.
            include_links: Also collect the page's outbound links. Off by
                default because a link-heavy page costs more to return than the
                caller may want.
            max_links: How many links to keep. ``FetchResult.links_total``
                reports how many there were before the cap.
            decline_cookies: Per-call consent override; None uses the config.
            ready_selector: Optional CSS selector or snapshot ref that must
                become visible after consent dismissal. Uses the navigation
                timeout separately.
            solve_cloudflare: Per-call override of
                ``SessionConfig.solve_cloudflare``; None uses the config. When
                on, Cloudflare's Turnstile checkbox is clicked and the real page
                is awaited for up to 60 s, apart from the navigation timeout.
            solve_datadome: Per-call override of
                ``SessionConfig.solve_datadome``; None uses the config. When on,
                DataDome's slider CAPTCHA is dragged and the real page is
                awaited for up to 60 s, apart from the navigation timeout.
            solve_perimeterx: Per-call override of
                ``SessionConfig.solve_perimeterx``; None uses the config. When
                on, HUMAN's (PerimeterX) Press & Hold button is held and the
                real page is awaited for up to 60 s, apart from the navigation
                timeout.
            solve_recaptcha: Per-call override of
                ``SessionConfig.solve_recaptcha``; None uses the config. When
                on, a reCAPTCHA v2 or hCaptcha checkbox on a 403, 429 or 503
                page is clicked and the real page is awaited for up to 60 s,
                apart from the navigation timeout.

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
        page = await self.new_page(text_only=text_only and not screenshot)
        documents = record_documents(page)
        try:
            deadline = time.monotonic() + timeout_s
            response = await page.goto(url, wait_until=wait_until, timeout_s=timeout_s)
            response = await settle(
                page,
                documents,
                response,
                wait_until=wait_until,
                deadline=deadline,
                solve_cloudflare=(
                    self.config.solve_cloudflare if solve_cloudflare is None else solve_cloudflare
                ),
                solve_datadome=(
                    self.config.solve_datadome if solve_datadome is None else solve_datadome
                ),
                solve_perimeterx=(
                    self.config.solve_perimeterx if solve_perimeterx is None else solve_perimeterx
                ),
                solve_recaptcha=(
                    self.config.solve_recaptcha if solve_recaptcha is None else solve_recaptcha
                ),
            )
            headers = response_headers(response)
            status = response_status(response)
            await raise_for_challenge(page, headers, status, deadline=deadline)
            declined = await self._decline_after_navigation(
                page, wait_until, decline_cookies=decline_cookies
            )
            if ready_selector is not None:
                await _wait_visible(
                    page, ready_selector, timeout_s=timeout_s, headers=headers, status=status
                )
            await raise_for_challenge(page, headers, status, deadline=deadline)
            state = await page.evaluate(
                _EXTRACT_SCRIPT,
                {
                    "text_only": text_only,
                    "max_links": (
                        max_links if include_links and self.config.java_script_enabled else None
                    ),
                },
            )
            screenshot_path = await _maybe_screenshot(page, screenshot)
            cookies = await self.cookies()
            return FetchResult(
                url=url,
                final_url=(
                    response.get("url", url)
                    if state["url"] == "chrome-error://chromewebdata/"
                    else state["url"]
                ),
                status=status,
                ok=_http_ok(status),
                headers=headers,
                text=state["text"],
                title=state["title"],
                cookies=cookies,
                timings={"elapsed_ms": (time.perf_counter() - started) * 1000},
                screenshot_path=screenshot_path,
                cookie_notice_declined=declined,
                links=[Link(**link) for link in state["links"]],
                links_total=state["links_total"],
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
                page,
                documents,
                response,
                wait_until="domcontentloaded",
                deadline=deadline,
                solve_cloudflare=self.config.solve_cloudflare,
                solve_datadome=self.config.solve_datadome,
                solve_perimeterx=self.config.solve_perimeterx,
                solve_recaptcha=self.config.solve_recaptcha,
            )
            await raise_for_challenge(
                page, response_headers(response), response_status(response), deadline=deadline
            )
            declined = await self._decline_after_navigation(page, "domcontentloaded")
            state = await page.evaluate(_EXTRACT_SCRIPT, {"text_only": False, "max_links": None})
        finally:
            documents.stop()
            with CancelScope(shield=True):
                await self.close_page(page)
        hits = parse_results(engine, state["text"])
        status = response_status(response)
        return SearchResult(
            query=query,
            engine=engine,
            url=url,
            final_url=state["url"],
            status=status,
            ok=_http_ok(status),
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


def _http_ok(status: int | None) -> bool:
    return status is not None and 200 <= status < 400


async def _wait_visible(
    page: CDPPage, target: str, *, timeout_s: float, headers: dict[str, str], status: int | None
) -> None:
    """Wait for ``target`` (CSS or ref) to be visible; check for a challenge on timeout."""

    async def visible() -> bool:
        return await cdp_dom.element_state(page, target) == "visible"

    try:
        await poll(visible, timeout_s * 1000, f"{target} to be visible")
    except WebSkrapError as exc:
        if exc.code is not ErrorCode.TIMEOUT:
            raise
        # A challenge can replace the page while readiness is pending.
        await raise_for_challenge(page, headers, status, deadline=time.monotonic())
        raise


async def _wait_attached(page: CDPPage, selector: str, *, timeout_s: float) -> None:
    async def attached() -> bool:
        return await cdp_dom.element_state(page, selector) != "detached"

    await poll(attached, timeout_s * 1000, selector)


async def _maybe_screenshot(page: CDPPage, screenshot: bool | str | Path) -> Path | None:
    if not screenshot:
        return None
    path = Path(f"webskrap-{uuid4().hex}.png") if screenshot is True else Path(screenshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = await page.screenshot(full_page=True)
    await asyncio.to_thread(path.write_bytes, image)
    return path
