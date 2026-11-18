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
from collections.abc import Iterable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from anyio import CancelScope

from webskrap.browser.channels import _fallback_channels as _fallback_channels
from webskrap.browser.channels import _resolution_locks as _resolution_locks
from webskrap.browser.channels import _resolved_channels as _resolved_channels
from webskrap.browser.channels import with_channel_fallback as with_channel_fallback
from webskrap.browser.display import VirtualDisplay
from webskrap.browser.executable import find_executable
from webskrap.browser.gpu import DRM_CLASS_DIR as DRM_CLASS_DIR
from webskrap.browser.gpu import HARDWARE_GPU_VENDORS as HARDWARE_GPU_VENDORS
from webskrap.browser.gpu import NVIDIA_DEVICE as NVIDIA_DEVICE
from webskrap.browser.gpu import VULKAN_ICD_DIRS as VULKAN_ICD_DIRS
from webskrap.browser.gpu import effective_gpu as effective_gpu
from webskrap.browser.gpu import hardware_gpu_available as hardware_gpu_available
from webskrap.browser.gpu import lavapipe_available as lavapipe_available
from webskrap.browser.gpu import require_lavapipe as require_lavapipe
from webskrap.browser.probe import browser_doctor as browser_doctor
from webskrap.config.models import (
    BrowserProfile,
    GpuBackend,
    SearchEngine,
    SessionConfig,
    WaitUntil,
)
from webskrap.config.profiles import get_profile
from webskrap.driver import cdp_setup
from webskrap.driver.cdp import launch
from webskrap.errors import (
    ErrorCode,
    WebSkrapError,
)
from webskrap.scraping.results import FetchResult, SearchResult
from webskrap.scraping.session import _EXTRACT_SCRIPT as _EXTRACT_SCRIPT
from webskrap.scraping.session import LINKS_SCRIPT as LINKS_SCRIPT
from webskrap.scraping.session import WebSkrapSession as WebSkrapSession
from webskrap.security.urls import validate_url

logger = logging.getLogger("webskrap.client")

T = TypeVar("T")


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
        solve_cloudflare: bool | None = None,
        solve_datadome: bool | None = None,
        solve_perimeterx: bool | None = None,
        solve_recaptcha: bool | None = None,
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
            solve_cloudflare: Per-call Turnstile override; see
                :meth:`WebSkrapSession.fetch`.
            solve_datadome: Per-call DataDome slider override; see
                :meth:`WebSkrapSession.fetch`.
            solve_perimeterx: Per-call Press & Hold override; see
                :meth:`WebSkrapSession.fetch`.
            solve_recaptcha: Per-call reCAPTCHA/hCaptcha checkbox override; see
                :meth:`WebSkrapSession.fetch`.

        Returns:
            A :class:`~webskrap.models.FetchResult`.

        Raises:
            WebSkrapError: If the client is closing or the browser cannot start.
        """
        name = f"_single_{uuid4().hex}"
        try:
            session = await self.session(name, config=config, profile=profile)
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
                solve_cloudflare=solve_cloudflare,
                solve_datadome=solve_datadome,
                solve_perimeterx=solve_perimeterx,
                solve_recaptcha=solve_recaptcha,
            )
        finally:
            # A cancelled waiter can leave its shielded browser launch running.
            with CancelScope(shield=True):
                await self.close_session(name)

    async def fetch_many(
        self,
        urls: Iterable[str],
        *,
        profile: str | BrowserProfile | None = None,
        config: SessionConfig | None = None,
        wait_until: WaitUntil = "domcontentloaded",
        screenshot: bool = False,
        timeout_ms: float | None = None,
        text_only: bool = False,
        include_links: bool = False,
        max_links: int = 50,
        decline_cookies: bool | None = None,
        ready_selector: str | None = None,
    ) -> list[FetchResult]:
        """Fetch a batch in order, using one browser for the whole batch.

        Cookies and storage are shared within the batch. Each call creates a
        fresh session and closes it on success, error, or cancellation. An
        explicit ``config.user_data_dir`` can still share state on disk.
        Use :meth:`session` and :meth:`WebSkrapSession.fetch` for streaming
        results, per-page options, or state that must outlive a batch.

        Args:
            urls: Finite iterable of URLs. All URLs are checked before launch.
                An empty iterable returns an empty list without a browser.
            profile: Browser profile; see :meth:`fetch`.
            config: Session config; defaults to ``default_config``.
            wait_until: Load state to wait for on each page.
            screenshot: Save each page to its own generated PNG filename.
            timeout_ms: Navigation timeout override for each page.
            text_only: Return visible body text instead of page HTML.
            include_links: Also collect each page's outbound links.
            max_links: How many links to keep per page.
            decline_cookies: Per-page consent override; None uses the config.
            ready_selector: Selector to wait for on each page.

        Returns:
            Results in input order, including ordinary HTTP error responses.

        Raises:
            WebSkrapError: On invalid input, launch failure, or the first page
                error. Remaining URLs are not fetched and no partial list is
                returned. See :meth:`WebSkrapSession.fetch` for page errors.
        """
        if isinstance(urls, str):
            raise WebSkrapError(
                "urls must be an iterable of URLs, not a string", code=ErrorCode.USAGE
            )
        targets = [validate_url(url) for url in urls]
        if not targets:
            return []
        name = f"_batch_{uuid4().hex}"
        try:
            session = await self.session(name, config=config, profile=profile)
            results = []
            for url in targets:
                results.append(
                    await session.fetch(
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
                )
            return results
        finally:
            # A cancelled waiter can leave its shielded browser launch running.
            with CancelScope(shield=True):
                await self.close_session(name)

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
        try:
            session = await self.session(name, config=config, profile=profile)
            return await session.search(
                query, engine=engine, max_results=max_results, timeout_ms=timeout_ms
            )
        finally:
            with CancelScope(shield=True):
                await self.close_session(name)

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
            gpu = effective_gpu(config.gpu, virtual_display=config.uses_virtual_display())
            args = config.model_copy(update={"gpu": gpu}).browser_args(profile)
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
    # The probe is a browser too: keep the proxy and caller's network flags.
    args = config.browser_args()
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
