"""Per-page and per-browser setup for sessions on the CDP driver.

:func:`prepare_page` runs on every page a session opens, before it navigates:
profile emulation (see :meth:`SessionConfig.emulation_commands`), the
JavaScript and service-worker switches, resource blocking and proxy
credentials. :func:`apply_storage_state` restores cookies and
``localStorage`` once, when a session starts.

Proxy credentials are answered only to challenges whose source is the proxy.
A website asking for HTTP authentication gets the browser's default answer,
so the proxy password never reaches a site.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from webskrap.cdp import CDPBrowser, CDPPage
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.models import BrowserProfile, ResourcePolicy, SessionConfig

#: CDP resource types each policy blocks.
BLOCKED_TYPES: dict[ResourcePolicy, tuple[str, ...]] = {
    ResourcePolicy.ALL: (),
    ResourcePolicy.LITE: ("Image", "Font", "Media"),
    ResourcePolicy.DOCUMENTS: ("Image", "Font", "Media", "Stylesheet"),
}

_BLOCK_WORKERS = """if (navigator.serviceWorker) navigator.serviceWorker.register =
  async () => { throw new DOMException('Service workers are blocked', 'SecurityError'); };"""

# A blank document served for an origin while its localStorage is restored.
_BLANK_PAGE = base64.b64encode(b"<!doctype html><title></title>").decode()


async def prepare_page(page: CDPPage, config: SessionConfig, profile: BrowserProfile) -> None:
    """Apply ``config`` and ``profile`` to a fresh page before it navigates."""
    await page.enable_events()
    page.default_timeout_s = config.default_timeout_ms / 1000
    for method, params in config.emulation_commands(profile):
        await page.send(method, params)
    if not config.java_script_enabled:
        await page.send("Emulation.setScriptExecutionDisabled", {"value": True})
    if config.service_workers == "block":
        # Bypassing only routes requests around existing workers. As in
        # Playwright, registration is refused too; this is a page-visible
        # patch, so it runs only for callers who ask for "block".
        await page.send("Network.setBypassServiceWorker", {"bypass": True})
        await page.send("Page.addScriptToEvaluateOnNewDocument", {"source": _BLOCK_WORKERS})
    await _route(page, config)


async def _route(page: CDPPage, config: SessionConfig) -> None:
    """Block resource types and answer proxy authentication through ``Fetch``.

    Without credentials only the blocked types are paused, so other requests
    never wait on this process. With credentials every request is paused,
    because ``Fetch`` reports authentication only for requests it intercepts.
    """
    blocked = BLOCKED_TYPES[config.resource_policy]
    proxy = config.proxy
    credentials = (
        (proxy.username or "", proxy.password or "")
        if proxy is not None and (proxy.username or proxy.password)
        else None
    )
    if not blocked and credentials is None:
        return
    tasks: set[asyncio.Task[Any]] = set()

    def spawn(method: str, params: dict[str, Any]) -> None:
        task = asyncio.create_task(_send_quietly(page, method, params))
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

    page.connection.on("Fetch.requestPaused", on_paused, session_id=page.session_id)
    if credentials is not None:
        page.connection.on("Fetch.authRequired", on_auth, session_id=page.session_id)
        await page.send(
            "Fetch.enable", {"patterns": [{"urlPattern": "*"}], "handleAuthRequests": True}
        )
    else:
        await page.send(
            "Fetch.enable",
            {"patterns": [{"urlPattern": "*", "resourceType": kind} for kind in blocked]},
        )


async def _send_quietly(page: CDPPage, method: str, params: dict[str, Any]) -> None:
    # The page may navigate or close while a request is paused; its id is then
    # gone and the browser answers with an error nobody needs.
    try:
        await page.send(method, params)
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
    return (
        isinstance(cookie, dict)
        and _strings(cookie, "name", "value", "domain")
        and isinstance(cookie.get("path", "/"), str)
    )


def _valid_origin(origin: object) -> bool:
    if not isinstance(origin, dict) or not _strings(origin, "origin"):
        return False
    items = origin.get("localStorage", [])
    return (
        urlsplit(origin["origin"]).scheme in ("http", "https")
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
            task = asyncio.create_task(_send_quietly(page, "Fetch.fulfillRequest", reply))
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
