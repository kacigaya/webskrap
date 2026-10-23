"""One-shot page fetch on WebSkrap's own CDP driver (:mod:`webskrap.cdp`).

:func:`fetch` returns the same :class:`~webskrap.models.FetchResult` as
:meth:`webskrap.client.WebSkrapClient.fetch`, so callers and benchmarks can
compare the two drivers directly. It launches Chromium with the flags a
Patchright fetch gets, plus ``--disable-blink-features=AutomationControlled``,
because no driver patch hides ``navigator.webdriver`` here.

CDP domains used on the page: ``Page`` (navigation and lifecycle events),
``Network`` (the main document's status and headers), ``Fetch`` (only when a
resource policy blocks types, and then only for those types), and
``Runtime.evaluate`` in isolated worlds. ``Runtime.enable`` is never sent.

Unsupported :class:`~webskrap.models.SessionConfig` fields raise ``usage``
instead of being silently ignored; see :data:`UNSUPPORTED_FIELDS`.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from anyio import CancelScope

from webskrap.cdp import CDPBrowser, CDPError, CDPPage, find_executable, launch
from webskrap.challenges import raise_for_challenge
from webskrap.client import LINKS_SCRIPT, require_lavapipe
from webskrap.display import VirtualDisplay
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.models import (
    BrowserProfile,
    FetchResult,
    GpuBackend,
    Link,
    ResourcePolicy,
    SessionConfig,
    WaitUntil,
)
from webskrap.profiles import get_profile
from webskrap.urls import validate_url

#: Config fields this driver does not implement yet, with the value that means
#: "not requested". A config that sets any other value is rejected.
UNSUPPORTED_FIELDS: dict[str, Any] = {
    "browser": "chromium",
    "storage_state": None,
    "java_script_enabled": True,
    "service_workers": "allow",
    "slow_mo_ms": None,
    "mask_headless_user_agent": False,
    "patchright_focus_control": None,
    "decline_cookies": False,
    # Patchright's context profile also sets color scheme, reduced motion and
    # extra headers in the browser context, which this driver has no
    # equivalent for yet. Without it, both drivers ignore the profile alike.
    "patchright_context_profile": False,
}

#: CDP resource types each policy blocks.
_BLOCKED_TYPES: dict[ResourcePolicy, tuple[str, ...]] = {
    ResourcePolicy.ALL: (),
    ResourcePolicy.LITE: ("Image", "Font", "Media"),
    ResourcePolicy.DOCUMENTS: ("Image", "Font", "Media", "Stylesheet"),
}

#: CDP lifecycle event name for each load state.
_LIFECYCLE_EVENTS: dict[str, str] = {
    "domcontentloaded": "DOMContentLoaded",
    "load": "load",
    "networkidle": "networkIdle",
}

# Matches Playwright's page.content(): doctype plus the serialized document.
_CONTENT_SCRIPT = """() => {
  let html = '';
  if (document.doctype) html = new XMLSerializer().serializeToString(document.doctype);
  if (document.documentElement) html += document.documentElement.outerHTML;
  return html;
}"""

# Playwright's visibility rule: a non-empty box and not visibility:hidden.
_VISIBLE_SCRIPT = """(selector) => {
  const element = document.querySelector(selector);
  if (!element) return false;
  const box = element.getBoundingClientRect();
  return box.width > 0 && box.height > 0 && getComputedStyle(element).visibility !== 'hidden';
}"""

_READY_POLL_S = 0.1


def launch_args(config: SessionConfig, profile: BrowserProfile) -> tuple[list[str], dict[str, str]]:
    """Return the Chromium flags and environment for ``config``.

    Reuses :meth:`SessionConfig.launch_options` in Patchright mode, so both
    stealth drivers share one flag definition. Proxy settings become
    ``--proxy-server`` and ``--proxy-bypass-list``.

    Raises:
        WebSkrapError: ``usage`` for unsupported fields or proxy credentials.
    """
    for name, default in UNSUPPORTED_FIELDS.items():
        if getattr(config, name) != default:
            msg = f"driver='cdp' does not support {name}={getattr(config, name)!r} yet"
            raise WebSkrapError(msg, code=ErrorCode.USAGE)
    if config.proxy is not None and (config.proxy.username or config.proxy.password):
        msg = "driver='cdp' cannot authenticate to a proxy yet: pass a URL without credentials"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    try:
        options = config.model_copy(update={"driver": "patchright"}).launch_options(profile)
    except ValueError as exc:
        raise WebSkrapError(str(exc), code=ErrorCode.USAGE) from exc
    args = list(options.get("args", []))
    if not any(arg.startswith("--disable-blink-features") for arg in args):
        args.insert(0, "--disable-blink-features=AutomationControlled")
    if config.proxy is not None:
        args.append(f"--proxy-server={config.proxy.server}")
        if config.proxy.bypass:
            args.append(f"--proxy-bypass-list={config.proxy.bypass}")
    if config.ignore_https_errors:
        args.append("--ignore-certificate-errors")
    env = dict(options.get("env") or {})
    return args, env


async def fetch(
    url: str,
    *,
    config: SessionConfig | None = None,
    profile: str | BrowserProfile | None = None,
    wait_until: WaitUntil = "domcontentloaded",
    screenshot: bool | str | Path = False,
    timeout_ms: float | None = None,
    text_only: bool = False,
    include_links: bool = False,
    max_links: int = 50,
    ready_selector: str | None = None,
) -> FetchResult:
    """Fetch one URL in a throwaway Chromium driven over the CDP pipe.

    Arguments match :meth:`webskrap.client.WebSkrapClient.fetch`, except that
    ``ready_selector`` takes a CSS selector only (no Playwright ``text=`` or
    ``role=`` engines) and consent notices are not dismissed.

    Raises:
        WebSkrapError: ``usage`` for unsupported options or a bad selector,
            ``browser_launch``/``sandbox`` when Chromium does not start,
            ``navigation`` for a network error, ``timeout`` when a wait runs
            out, and ``blocked`` for a recognized bot challenge.
    """
    config = config or SessionConfig()
    url = validate_url(url)
    if ready_selector is not None and not ready_selector.strip():
        raise WebSkrapError("ready_selector cannot be blank", code=ErrorCode.USAGE)
    resolved_profile = profile if isinstance(profile, BrowserProfile) else get_profile(profile)
    args, env = launch_args(config, resolved_profile)
    executable = find_executable(config.channel)
    started = time.perf_counter()

    display: VirtualDisplay | None = None
    browser: CDPBrowser | None = None
    try:
        if config.gpu is GpuBackend.MESA:
            require_lavapipe()
        if config.uses_virtual_display():
            screen = config.virtual_screen()
            display = await VirtualDisplay.start(screen.width, screen.height)
            env = {**(env or os.environ), **display.env}
        browser = await launch(
            executable,
            args=args,
            headless=config.headless and not config.virtual_display,
            user_data_dir=config.user_data_dir,
            env=env or None,
            timeout_s=config.timeout_ms / 1000,
        )
        browser.connection.default_timeout_s = config.default_timeout_ms / 1000
        page = await browser.new_page()
        page.default_timeout_s = config.default_timeout_ms / 1000
        return await _fetch_page(
            browser,
            page,
            url,
            config=config,
            wait_until=wait_until,
            screenshot=screenshot,
            timeout_s=(timeout_ms or config.navigation_timeout_ms) / 1000,
            text_only=text_only,
            max_links=max_links if include_links else None,
            ready_selector=ready_selector,
            started=started,
        )
    finally:
        with CancelScope(shield=True):
            if browser is not None:
                await browser.close()
            if display is not None:
                await display.stop()


async def _fetch_page(
    browser: CDPBrowser,
    page: CDPPage,
    url: str,
    *,
    config: SessionConfig,
    wait_until: WaitUntil,
    screenshot: bool | str | Path,
    timeout_s: float,
    text_only: bool,
    max_links: int | None,
    ready_selector: str | None,
    started: float,
) -> FetchResult:
    await page.send("Page.enable")
    await page.send("Page.setLifecycleEventsEnabled", {"enabled": True})
    await page.send("Network.enable")
    blocked = _BLOCKED_TYPES[config.resource_policy]
    if blocked:
        _route_blocked(page)
        await page.send(
            "Fetch.enable",
            {"patterns": [{"urlPattern": "*", "resourceType": kind} for kind in blocked]},
        )

    events = page.record("Page.lifecycleEvent", "Network.responseReceived")
    # Navigation and its load wait share one budget, as in Playwright's goto.
    deadline = time.monotonic() + timeout_s
    try:
        navigation = await page.send("Page.navigate", {"url": url}, timeout_s=timeout_s)
        if navigation.get("errorText"):
            msg = f"{navigation['errorText']} at {url}"
            raise WebSkrapError(msg, code=ErrorCode.NAVIGATION)
        loader_id = navigation.get("loaderId")
        response: dict[str, Any] = {}
        if loader_id:
            if wait_until in _LIFECYCLE_EVENTS:
                name = _LIFECYCLE_EVENTS[wait_until]
                await events.wait(
                    lambda method, params: (
                        method == "Page.lifecycleEvent"
                        and params.get("loaderId") == loader_id
                        and params.get("name") == name
                    ),
                    timeout_s=max(0.0, deadline - time.monotonic()),
                    what=f"{wait_until} on {url}",
                )
            try:
                document = await events.wait(
                    lambda method, params: (
                        method == "Network.responseReceived"
                        and params.get("loaderId") == loader_id
                        and params.get("type") == "Document"
                    ),
                    # The response usually arrives before navigate returns.
                    # A load without a network response (about:, data:) has none.
                    timeout_s=min(timeout_s, 5.0),
                    what="the document response",
                )
                response = document.get("response", {})
            except WebSkrapError as exc:
                if exc.code is not ErrorCode.TIMEOUT:
                    raise
    finally:
        events.stop()

    headers = {str(key).lower(): str(value) for key, value in response.get("headers", {}).items()}
    status = response.get("status")
    status = int(status) if isinstance(status, int | float) else None
    await raise_for_challenge(page, headers)
    if ready_selector is not None:
        await _wait_visible(page, ready_selector, timeout_s=timeout_s, headers=headers)
        await raise_for_challenge(page, headers)

    state = await page.evaluate(
        "({ url: location.href, title: document.title, text: document.body ? "
        "document.body.innerText : '' })"
    )
    text = state["text"] if text_only else await page.evaluate(_CONTENT_SCRIPT)
    links: list[Link] = []
    links_total = 0
    if max_links is not None:
        collected = await page.evaluate(LINKS_SCRIPT, max_links)
        links = [Link(**link) for link in collected["links"]]
        links_total = int(collected["total"])
    screenshot_path = await _screenshot(page, screenshot)
    cookies = await _cookies(browser)
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
        links=links,
        links_total=links_total,
    )


def _route_blocked(page: CDPPage) -> None:
    """Fail every paused request; ``Fetch.enable`` pauses only blocked types."""
    tasks: set[asyncio.Task[Any]] = set()

    def on_paused(params: dict[str, Any]) -> None:
        body = {"requestId": params["requestId"], "errorReason": "BlockedByClient"}
        task = asyncio.create_task(_send_quietly(page, "Fetch.failRequest", body))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    page.connection.on("Fetch.requestPaused", on_paused, session_id=page.session_id)


async def _send_quietly(page: CDPPage, method: str, params: dict[str, Any]) -> None:
    # The page may navigate or close while a request is paused; its id is then
    # gone and the browser answers with an error nobody needs.
    try:
        await page.send(method, params)
    except WebSkrapError:
        return


async def _wait_visible(
    page: CDPPage, selector: str, *, timeout_s: float, headers: dict[str, str]
) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if await page.evaluate(_VISIBLE_SCRIPT, selector):
                return
        except CDPError as exc:
            if "is not a valid selector" in str(exc):
                msg = f"ready_selector is not a valid CSS selector: {selector}"
                raise WebSkrapError(msg, code=ErrorCode.USAGE) from exc
            # A navigation destroyed the world mid-call; poll again.
        if time.monotonic() >= deadline:
            # A challenge can replace the page while readiness is pending.
            await raise_for_challenge(page, headers)
            msg = f"timed out after {timeout_s * 1000:.0f}ms waiting for {selector} to be visible"
            raise WebSkrapError(msg, code=ErrorCode.TIMEOUT)
        await asyncio.sleep(_READY_POLL_S)


async def _screenshot(page: CDPPage, screenshot: bool | str | Path) -> Path | None:
    if not screenshot:
        return None
    path = Path(f"webskrap-{uuid4().hex}.png") if screenshot is True else Path(screenshot)
    metrics = await page.send("Page.getLayoutMetrics")
    size = metrics.get("cssContentSize") or metrics["contentSize"]
    shot = await page.send(
        "Page.captureScreenshot",
        {
            "format": "png",
            "captureBeyondViewport": True,
            "clip": {"x": 0, "y": 0, "width": size["width"], "height": size["height"], "scale": 1},
        },
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, base64.b64decode(shot["data"]))
    return path


async def _cookies(browser: CDPBrowser) -> list[dict[str, Any]]:
    """Return every cookie in the browser, shaped like Playwright's ``cookies()``."""
    result = await browser.connection.send("Storage.getCookies")
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
