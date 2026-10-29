"""Per-page and per-browser setup for sessions on the CDP driver.

:func:`prepare_page` runs on every page a session opens, before it navigates:
profile emulation (see :meth:`SessionConfig.emulation_commands`), the
JavaScript and service-worker switches, resource blocking and proxy
credentials. :func:`apply_storage_state` restores cookies and
``localStorage`` once, when a session starts.

The same setup covers targets the page starts: out-of-process iframes and
their workers attach paused, get the setup, and then run. Without it a
cross-site iframe reports the browser's own user agent, timezone and
languages, and loads what the resource policy blocks. Dedicated workers of a
page's own frames need only the identity; the frame's ``Fetch`` already sees
their requests. :func:`prepare_browser` does the same for shared and service
workers, which belong to the browser, not to a page.

Chromium refuses the device-metrics override below top-level pages, so an
iframe still reports the real screen size and pixel ratio. A shared worker
keeps the browser's own ``navigator.userAgent``; it is read before the
override arrives. ``mask_headless_user_agent`` sets the user agent for the
whole browser. Workers also keep their native ``navigator.platform``.
Desktop full profiles resize the browser window to fit the requested viewport.

Proxy credentials are answered only to challenges whose source is the proxy.
A website asking for HTTP authentication gets the browser's default answer,
so the proxy password never reaches a site.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from webskrap import cdp_dom
from webskrap.cdp import CDPBrowser, CDPConnection, CDPError, CDPPage
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.models import BrowserProfile, ResourcePolicy, SessionConfig, user_agent_metadata

#: CDP resource types each policy blocks.
BLOCKED_TYPES: dict[ResourcePolicy, tuple[str, ...]] = {
    ResourcePolicy.ALL: (),
    ResourcePolicy.LITE: ("Image", "Font", "Media"),
    ResourcePolicy.DOCUMENTS: ("Image", "Font", "Media", "Stylesheet"),
}

_BLOCK_WORKERS = """if (navigator.serviceWorker) navigator.serviceWorker.register =
  async () => { throw new DOMException('Service workers are blocked', 'SecurityError'); };"""

#: Emulation Chromium runs on top-level pages only.
_TOP_LEVEL_ONLY = frozenset({"Emulation.setDeviceMetricsOverride"})
#: Emulation Chromium refuses on workers. A worker takes its locale from
#: whoever started it, so the locale override is refused there too.
_NOT_ON_WORKERS = _TOP_LEVEL_ONLY | {
    "Emulation.setTouchEmulationEnabled",
    "Emulation.setEmulatedMedia",
    "Emulation.setLocaleOverride",
}
_WORKER_TYPES = frozenset({"worker", "shared_worker", "service_worker"})

# A blank document served for an origin while its localStorage is restored.
_BLANK_PAGE = base64.b64encode(b"<!doctype html><title></title>").decode()


async def prepare_page(page: CDPPage, config: SessionConfig, profile: BrowserProfile) -> None:
    """Apply ``config`` and ``profile`` to a fresh page before it navigates."""
    await page.enable_events()
    page.default_timeout_s = config.default_timeout_ms / 1000
    if config.profile_mode == "full" and not profile.is_mobile:
        await _fit_window(page, profile)
    commands = await _emulation(page.connection, config, profile)
    for method, params in commands:
        await page.send(method, params)
    await _prepare_document(page, config)
    await _route(page, config)
    if commands or _routes(config) or _document_switches(config):
        await cdp_dom.start_auto_attach(
            page, _on_attached(page.connection, page.default_timeout_s, config, commands)
        )


async def _fit_window(page: CDPPage, profile: BrowserProfile) -> None:
    """Fit a desktop viewport inside a real browser window before emulation."""
    window = await page.connection.send("Browser.getWindowForTarget", {"targetId": page.target_id})
    try:
        await page.connection.send(
            "Browser.setContentsSize",
            {
                "windowId": window["windowId"],
                "width": profile.viewport.width,
                "height": profile.viewport.height,
            },
        )
    except CDPError as exc:
        # Older Chromium versions lack this experimental browser command.
        if not str(exc).startswith("CDP error -32601:"):
            raise


#: The brand each channel's browser reports next to ``Chromium``.
_CHANNEL_BRANDS: dict[str | None, str] = {"chrome": "Google Chrome", "msedge": "Microsoft Edge"}


async def _emulation(
    connection: CDPConnection, config: SessionConfig, profile: BrowserProfile
) -> list[tuple[str, dict[str, Any]]]:
    """Return ``config``'s emulation commands, with client hints for a profile UA.

    The hints need the running browser's version, so ``Browser.getVersion``
    is asked only when a user agent is overridden.
    """
    metadata = None
    if config.profile_mode == "full" and profile.user_agent:
        version = await connection.send("Browser.getVersion")
        full_version = str(version.get("product", "")).rpartition("/")[2]
        metadata = user_agent_metadata(
            profile.user_agent,
            brand=_CHANNEL_BRANDS.get(config.channel),
            full_version=full_version,
        )
    return config.emulation_commands(profile, user_agent_metadata=metadata)


async def prepare_browser(
    browser: CDPBrowser, config: SessionConfig, profile: BrowserProfile
) -> None:
    """Give shared and service workers the session's identity and routing.

    They are browser targets, so a page's auto-attach does not report them.
    Each one waits for the debugger until it is set up.
    """
    connection = browser.connection
    commands = await _emulation(connection, config, profile)
    if not commands and not _routes(config):
        return
    timeout_s = config.default_timeout_ms / 1000
    handler = _on_attached(connection, timeout_s, config, commands)
    connection.on("Target.attachedToTarget", handler, session_id=None)
    await connection.send(
        "Target.setAutoAttach",
        {
            "autoAttach": True,
            "waitForDebuggerOnStart": True,
            "flatten": True,
            "filter": [{"type": "shared_worker"}, {"type": "service_worker"}, {"exclude": True}],
        },
    )


def _on_attached(
    connection: CDPConnection,
    timeout_s: float,
    config: SessionConfig,
    commands: list[tuple[str, dict[str, Any]]],
) -> Callable[[dict[str, Any]], None]:
    """Return a ``Target.attachedToTarget`` handler that sets up and resumes targets.

    ``commands`` are the page's emulation commands, from :func:`_emulation`.
    """
    tasks: set[asyncio.Task[None]] = set()

    def attached(params: dict[str, Any]) -> None:
        info = params.get("targetInfo", {})
        child = CDPPage(connection, params["sessionId"], info.get("targetId", ""))
        child.default_timeout_s = timeout_s
        task = asyncio.create_task(_prepare_target(child, info.get("type"), config, commands))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    return attached


async def _prepare_target(
    child: CDPPage,
    kind: str | None,
    config: SessionConfig,
    commands: list[tuple[str, dict[str, Any]]],
) -> None:
    """Set up one attached iframe or worker, then let it run.

    The target waits until ``Runtime.runIfWaitingForDebugger``, which is sent
    even when setup fails, so a target that closed early never hangs a page.
    """
    try:
        # Routing first: a refused override must not let blocked types through.
        with suppress(WebSkrapError):
            if kind == "iframe":
                await _prepare_document(child, config)
                await _route(child, config)
                handler = _on_attached(child.connection, child.default_timeout_s, config, commands)
                await cdp_dom.start_auto_attach(child, handler)
            elif kind in ("shared_worker", "service_worker"):
                await _route(child, config)
        with suppress(WebSkrapError):
            skipped = _NOT_ON_WORKERS if kind in _WORKER_TYPES else _TOP_LEVEL_ONLY
            for method, params in commands:
                if method not in skipped:
                    await child.send(method, params)
    finally:
        with suppress(WebSkrapError):
            await child.send("Runtime.runIfWaitingForDebugger")


def _document_switches(config: SessionConfig) -> bool:
    return not config.java_script_enabled or config.service_workers == "block"


async def _prepare_document(page: CDPPage, config: SessionConfig) -> None:
    """Apply the JavaScript and service-worker switches to a page or iframe."""
    if not config.java_script_enabled:
        await page.send("Emulation.setScriptExecutionDisabled", {"value": True})
    if config.service_workers == "block":
        # Bypassing only routes requests around existing workers. As in
        # Playwright, registration is refused too; this is a page-visible
        # patch, so it runs only for callers who ask for "block".
        await page.send("Network.setBypassServiceWorker", {"bypass": True})
        await page.send("Page.addScriptToEvaluateOnNewDocument", {"source": _BLOCK_WORKERS})


def _credentials(config: SessionConfig) -> tuple[str, str] | None:
    proxy = config.proxy
    if proxy is None or not (proxy.username or proxy.password):
        return None
    return proxy.username or "", proxy.password or ""


def _routes(config: SessionConfig) -> bool:
    """True when :func:`_route` has anything to do for ``config``."""
    return bool(BLOCKED_TYPES[config.resource_policy]) or _credentials(config) is not None


async def _route(page: CDPPage, config: SessionConfig) -> None:
    """Block resource types and answer proxy authentication through ``Fetch``."""
    await intercept(
        page.connection,
        page.session_id,
        blocked=BLOCKED_TYPES[config.resource_policy],
        credentials=_credentials(config),
    )


async def intercept(
    connection: CDPConnection,
    session_id: str | None,
    *,
    blocked: tuple[str, ...] = (),
    credentials: tuple[str, str] | None = None,
) -> None:
    """Fail ``blocked`` resource types and answer proxy authentication.

    ``session_id`` None intercepts at the browser target, which covers every
    page, frame and worker of the browser. Without credentials only the
    blocked types are paused, so other requests never wait on this process.
    With credentials every request is paused, because ``Fetch`` reports
    authentication only for requests it intercepts.
    """
    if not blocked and credentials is None:
        return
    tasks: set[asyncio.Task[Any]] = set()

    def spawn(method: str, params: dict[str, Any]) -> None:
        task = asyncio.create_task(_send_quietly(connection, session_id, method, params))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def on_paused(params: dict[str, Any]) -> None:
        request = {"requestId": params["requestId"]}
        if params.get("resourceType") in blocked:
            spawn("Fetch.failRequest", {**request, "errorReason": "BlockedByClient"})
        else:
            spawn("Fetch.continueRequest", request)

    def on_auth(params: dict[str, Any]) -> None:
        challenge = params.get("authChallenge", {})
        if credentials is not None and challenge.get("source") == "Proxy":
            username, password = credentials
            answer = {"response": "ProvideCredentials", "username": username, "password": password}
        else:
            # A site's own login prompt: never hand it the proxy password.
            answer = {"response": "Default"}
        spawn(
            "Fetch.continueWithAuth",
            {"requestId": params["requestId"], "authChallengeResponse": answer},
        )

    connection.on("Fetch.requestPaused", on_paused, session_id=session_id)
    if credentials is not None:
        connection.on("Fetch.authRequired", on_auth, session_id=session_id)
        params: dict[str, Any] = {"patterns": [{"urlPattern": "*"}], "handleAuthRequests": True}
    else:
        params = {"patterns": [{"urlPattern": "*", "resourceType": kind} for kind in blocked]}
    await connection.send("Fetch.enable", params, session_id=session_id)


async def _send_quietly(
    connection: CDPConnection, session_id: str | None, method: str, params: dict[str, Any]
) -> None:
    # The page may navigate or close while a request is paused; its id is then
    # gone and the browser answers with an error nobody needs.
    try:
        await connection.send(method, params, session_id=session_id)
    except WebSkrapError:
        return


def load_storage_state(state: Path | dict[str, Any]) -> dict[str, Any]:
    """Return a Playwright-format storage state from a dict or a JSON file.

    Raises:
        WebSkrapError: ``usage`` when the file cannot be read or is not a
            ``{"cookies": [...], "origins": [...]}`` object.
    """
    if isinstance(state, Path):
        try:
            state = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            msg = f"storage_state could not be read: {exc}"
            raise WebSkrapError(msg, code=ErrorCode.USAGE) from exc
    msg = "storage_state must be an object with cookies and origins"
    if not isinstance(state, dict):
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    cookies = state.get("cookies", [])
    origins = state.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    if not all(_valid_cookie(cookie) for cookie in cookies):
        msg = "storage_state cookies need string name and value, and a domain"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    if not all(_valid_origin(origin) for origin in origins):
        msg = (
            "storage_state origins need an http(s) origin and localStorage entries "
            "with string name and value"
        )
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    return state


def _strings(entry: dict[str, Any], *keys: str) -> bool:
    return all(isinstance(entry.get(key), str) for key in keys)


def _valid_cookie(cookie: object) -> bool:
    if not isinstance(cookie, dict) or not _strings(cookie, "name", "value", "domain"):
        return False
    expires = cookie.get("expires", -1)
    return (
        isinstance(cookie.get("path", "/"), str)
        and all(isinstance(cookie.get(key, False), bool) for key in ("secure", "httpOnly"))
        and isinstance(expires, int | float)
        and not isinstance(expires, bool)
        and cookie.get("sameSite", "Lax") in ("Strict", "Lax", "None")
    )


def _valid_origin(origin: object) -> bool:
    if not isinstance(origin, dict) or not _strings(origin, "origin"):
        return False
    try:
        url = urlsplit(origin["origin"])
        _ = url.port  # raises on an invalid port
    except ValueError:
        return False
    items = origin.get("localStorage", [])
    return (
        url.scheme in ("http", "https")
        and bool(url.hostname)
        and url.username is None
        and url.path in ("", "/")
        and not url.query
        and not url.fragment
        and isinstance(items, list)
        and all(isinstance(item, dict) and _strings(item, "name", "value") for item in items)
    )


def _cookie_param(cookie: dict[str, Any]) -> dict[str, Any]:
    """Convert one Playwright-format cookie to a CDP ``CookieParam``."""
    param = {
        key: cookie[key]
        for key in ("name", "value", "domain", "path", "secure", "httpOnly")
        if key in cookie
    }
    expires = cookie.get("expires", -1)
    if isinstance(expires, int | float) and expires > 0:
        param["expires"] = expires
    if cookie.get("sameSite") in ("Strict", "Lax", "None"):
        param["sameSite"] = cookie["sameSite"]
    return param


async def apply_storage_state(browser: CDPBrowser, state: Path | dict[str, Any]) -> None:
    """Restore cookies and ``localStorage`` from a Playwright storage state.

    Cookies go in with ``Storage.setCookies``. For each origin with
    ``localStorage``, a throwaway page opens the origin with its document
    served from here, never fetched, writes the entries from an isolated
    world, and closes. Nothing stays injected into later pages.
    """
    data = load_storage_state(state)
    cookies = [_cookie_param(cookie) for cookie in data.get("cookies", [])]
    if cookies:
        await browser.connection.send("Storage.setCookies", {"cookies": cookies})
    for origin in data.get("origins", []):
        entries = [
            [item["name"], item["value"]]
            for item in origin.get("localStorage", [])
            if "name" in item and "value" in item
        ]
        if entries:
            await _write_local_storage(browser, str(origin["origin"]), entries)


async def _write_local_storage(browser: CDPBrowser, origin: str, entries: list[list[str]]) -> None:
    page = await browser.new_page()
    tasks: set[asyncio.Task[Any]] = set()
    try:

        def serve_blank(params: dict[str, Any]) -> None:
            reply = {"requestId": params["requestId"], "responseCode": 200, "body": _BLANK_PAGE}
            task = asyncio.create_task(
                _send_quietly(page.connection, page.session_id, "Fetch.fulfillRequest", reply)
            )
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.connection.on("Fetch.requestPaused", serve_blank, session_id=page.session_id)
        await page.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
        await page.goto(origin.rstrip("/") + "/", wait_until="load", timeout_s=30)
        await page.evaluate(
            "(entries) => { for (const [k, v] of entries) localStorage.setItem(k, v); }",
            entries,
        )
    finally:
        await browser.connection.send(
            "Target.closeTarget", {"targetId": page.target_id}, timeout_s=5
        )
