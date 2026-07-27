from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from random import uniform
from typing import Any
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Page, Playwright

from webskrap.consent import SETTLED_PAGE_TIMEOUT_MS
from webskrap.consent import decline_cookies as _decline_cookies
from webskrap.models import BrowserProfile, FetchResult, ResourcePolicy, SessionConfig, WaitUntil
from webskrap.profiles import get_profile


def _async_playwright(driver: str):
    """Return the async_playwright factory for the chosen driver.

    ``patchright`` is a drop-in, API-compatible fork of Playwright that hides the
    CDP ``Runtime.enable`` leak used by CDP-aware bot detectors. It requires the
    ``patchright`` ships with WebSkrap, but needs its browser download
    (``webskrap install``).
    """
    if driver == "patchright":
        try:
            from patchright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            msg = "driver='patchright' requires patchright. Run: pip install webskrap"
            raise WebSkrapError(msg) from exc
        return async_playwright()
    from playwright.async_api import async_playwright

    return async_playwright()


class WebSkrapError(RuntimeError):
    pass


class WebSkrapSession:
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
        self.name = name
        self.context = context
        self.config = config
        self.profile = profile
        self.browser = browser
        self._temp_user_data_dir = temp_user_data_dir
        self._closed = False

    async def __aenter__(self) -> WebSkrapSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    async def fetch(
        self,
        url: str,
        *,
        wait_until: WaitUntil = "domcontentloaded",
        screenshot: bool | str | Path = False,
        timeout_ms: float | None = None,
        text_only: bool = False,
    ) -> FetchResult:
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
            screenshot_path = await _maybe_screenshot(page, screenshot)
            cookies = await self.context.cookies()
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
            raise WebSkrapError(msg)

        box = await locator.bounding_box(timeout=timeout)
        if box is None:
            msg = f"could not find a visible bounding box for selector: {selector}"
            raise WebSkrapError(msg)

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
        if self._closed:
            return
        self._closed = True
        await self.context.close()
        if self.browser is not None:
            await self.browser.close()
        if self._temp_user_data_dir is not None:
            shutil.rmtree(self._temp_user_data_dir, ignore_errors=True)
            self._temp_user_data_dir = None

    def _ensure_open(self) -> None:
        if self._closed:
            msg = f"session '{self.name}' is closed"
            raise WebSkrapError(msg)


class WebSkrapClient:
    def __init__(
        self,
        *,
        default_config: SessionConfig | None = None,
        profiles: Mapping[str, BrowserProfile] | None = None,
    ) -> None:
        self.default_config = default_config or SessionConfig()
        self.profiles = dict(profiles or {})
        self._playwright_manager = None
        self._playwright: Playwright | None = None
        self._sessions: dict[str, WebSkrapSession] = {}

    async def __aenter__(self) -> WebSkrapClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._playwright is not None:
            return
        self._playwright_manager = _async_playwright(self.default_config.driver)
        self._playwright = await self._playwright_manager.start()

    async def close(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()
        if self._playwright is not None:
            await self._playwright.stop()
        self._playwright_manager = None
        self._playwright = None

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
    ) -> FetchResult:
        name = f"_single_{uuid4().hex}"
        session = await self.session(name, config=config, profile=profile)
        try:
            return await session.fetch(
                url,
                wait_until=wait_until,
                screenshot=screenshot,
                timeout_ms=timeout_ms,
                text_only=text_only,
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
        if name in self._sessions:
            return self._sessions[name]
        await self.start()
        resolved_config = config or self.default_config
        resolved_profile = self._resolve_profile(profile)
        session = await self._create_session(name, resolved_config, resolved_profile)
        self._sessions[name] = session
        return session

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

        if user_data_dir is not None:
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context = await browser_type.launch_persistent_context(
                str(user_data_dir),
                **launch_options,
                **context_options,
            )
            browser = None
        else:
            browser = await browser_type.launch(**launch_options)
            context = await browser.new_context(**context_options)

        context.set_default_timeout(config.default_timeout_ms)
        context.set_default_navigation_timeout(config.navigation_timeout_ms)

        if config.resource_policy != ResourcePolicy.ALL:
            await context.route("**/*", _resource_route_handler(config.resource_policy))
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
    box: dict[str, float],
    position: dict[str, float] | None,
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
    path = (
        Path(f"webskrap-{int(time.time() * 1000)}.png") if screenshot is True else Path(screenshot)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(path), full_page=True)
    return path
