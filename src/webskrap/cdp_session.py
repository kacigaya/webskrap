"""Persistent-session page operations on WebSkrap's own CDP driver.

:func:`attach` reconnects to a session's running browser over the WebSocket on
its debugging port, the same port Patchright attaches to. The other functions
mirror :mod:`webskrap.browser_session`'s page operations for a
:class:`~webskrap.cdp.CDPPage`; that module dispatches here when a session was
opened with ``driver="cdp"``.

Like the fetch backend, nothing here sends ``Runtime.enable``. Reads run in
isolated worlds. Only :func:`evaluate` touches the page's own world, because a
caller of ``eval`` expects what page scripts see.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from webskrap import cdp_dom, challenges, consent
from webskrap.cdp import CDPConnection, CDPError, CDPPage, WebSocketTransport
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.models import ElementState, LoadState
from webskrap.urls import validate_url

if TYPE_CHECKING:
    from webskrap.browser_session import BrowserView, Viewport

_STATE_SCRIPT = "({ url: location.href, title: document.title })"
#: How long a browser navigation waits for a consent notice. Longer than the
#: fetch default (2 s): some CMPs inject their notice 7 s after load. The wait
#: ends as soon as a notice shows, so only opted-in pages without one pay it.
DECLINE_TIMEOUT_MS = 10_000
_VIEWPORT_SCRIPT = (
    "({ width: innerWidth, height: innerHeight, scroll_x: scrollX, scroll_y: scrollY, "
    "dpr: devicePixelRatio })"
)
# Same window as browser_session.read_text: code points, not UTF-16 units.
_TEXT_SCRIPT = """({limit, start}) => {
  const text = document.body ? document.body.innerText : '';
  const end = start + limit;
  const selected = [];
  let length = 0;
  for (const character of text) {
    if (length >= start && length < end) selected.push(character);
    length++;
  }
  const offset = Math.min(start, length);
  const stop = Math.min(end, length);
  return {
    title: document.title,
    text: selected.join(''),
    text_length: length,
    text_offset: offset,
    text_truncated: stop < length,
    next_text_offset: stop < length ? stop : null,
  };
}"""
# Mirrors Playwright's get_by_text default: case-insensitive substring of
# whitespace-normalized text in a visible element.
_TEXT_VISIBLE_SCRIPT = """({needle}) => {
  const want = needle.replace(/\\s+/g, ' ').trim().toLowerCase();
  const walker = document.createTreeWalker(document.body || document, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const element = node.parentElement;
    if (!element) continue;
    const text = (element.innerText || element.textContent || '')
      .replace(/\\s+/g, ' ').trim().toLowerCase();
    if (!text.includes(want)) continue;
    const box = element.getBoundingClientRect();
    if (box.width > 0 && box.height > 0 && getComputedStyle(element).visibility !== 'hidden')
      return true;
  }
  return false;
}"""
# Serializes an eval result the way Patchright's evaluate reports it: windows
# and DOM nodes become "ref: <Window>" / "ref: <Node>" placeholders, and a
# reference back into the object's own path becomes "[Circular]" (Patchright
# rebuilds the cycle; JSON cannot carry one). A function value is called
# first, as Playwright calls an expression that evaluates to a function.
_SERIALIZE_FUNCTION = """async function () {
  const value = typeof this === 'function' ? await this() : this;
  const path = new Set();
  const walk = (item) => {
    if (item === undefined || typeof item === 'function' || typeof item === 'symbol') return null;
    if (typeof item === 'bigint') return String(item);
    if (typeof item === 'number' && !Number.isFinite(item)) return String(item);
    if (item === null || typeof item !== 'object') return item;
    if (typeof Window !== 'undefined' && item instanceof Window) return 'ref: <Window>';
    if (typeof Node !== 'undefined' && item instanceof Node) return 'ref: <Node>';
    if (item instanceof Date) return item.toJSON();
    if (path.has(item)) return '[Circular]';
    path.add(item);
    let out;
    if (Array.isArray(item)) out = item.map(walk);
    else {
      out = {};
      for (const key of Object.keys(item)) out[key] = walk(item[key]);
    }
    path.delete(item);
    return out;
  };
  return walk(value);
}"""
#: CDP ``unserializableValue`` strings for primitives JSON cannot carry.
_UNSERIALIZABLE: dict[str, float] = {
    "NaN": float("nan"),
    "Infinity": float("inf"),
    "-Infinity": float("-inf"),
    "-0": -0.0,
}
_POLL_S = 0.1
T = TypeVar("T")
#: Session-directory file with page target ids in creation order.
PAGE_ORDER_FILE = "cdp-pages.json"
#: Session-directory file with the selected tab and the pages known then.
TAB_SELECTION_FILE = "cdp-tab.json"
# networkidle has no live event to subscribe to after the fact; this quiet
# window matches the 500 ms Chromium's own networkIdle lifecycle event uses.
_NETWORK_QUIET_S = 0.5


def devtools_path(user_data_dir: Path, port: int) -> str:
    """Return the browser WebSocket path Chromium wrote for ``port``.

    Chromium writes ``DevToolsActivePort`` as the port on line one and the
    browser target path on line two. The path holds a random id, so it cannot
    be guessed.
    """
    try:
        lines = (user_data_dir / "DevToolsActivePort").read_text(encoding="utf-8").split()
    except OSError:
        lines = []
    if len(lines) < 2 or lines[0] != str(port):
        msg = "session browser has no matching DevToolsActivePort file"
        raise WebSkrapError(msg, code=ErrorCode.SESSION_UNREACHABLE)
    return lines[1]


def _ordered_pages(order_file: Path, current: list[str]) -> list[str]:
    """Return open page target ids, oldest first, and remember the order.

    ``Target.getTargets`` lists targets in no defined order, and each command
    is a new connection, so creation order is kept in ``order_file``: known
    pages keep their place, closed ones drop out, and new ones go last.
    Patchright's ``context.pages[-1]`` is then the last entry: the newest
    page, such as a popup the previous command opened. :func:`attach` records
    pages created while it is attached in creation order. Several pages
    opened while no command runs have no known order, as with Patchright,
    which reattaches for every command too.
    """
    try:
        known = json.loads(order_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = []
    if not isinstance(known, list):
        known = []
    ordered = [target for target in known if target in current]
    ordered += [target for target in current if target not in ordered]
    if ordered != known:
        _write_ids(order_file, ordered)
    return ordered


def _write_ids(path: Path, data: object) -> None:
    temp = path.with_suffix(".tmp")
    # Owner-only like the rest of the session directory; ids only, no data.
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temp, path)


def current_page(directory: Path, pages: list[str]) -> str | None:
    """Return the page commands act on, from ``pages`` in creation order.

    That is the newest page opened after the last tab selection, such as a
    popup or a new tab; else the selected tab while it is open; else the
    newest page, as Patchright's ``context.pages[-1]``.
    """
    try:
        selection = json.loads((directory / TAB_SELECTION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        selection = None
    if not isinstance(selection, dict):
        return pages[-1] if pages else None
    known = selection.get("known")
    newer = [page for page in pages if not isinstance(known, list) or page not in known]
    if newer:
        return newer[-1]
    target = selection.get("target")
    return target if target in pages else (pages[-1] if pages else None)


def _select(directory: Path, target: str, pages: list[str]) -> None:
    _write_ids(directory / TAB_SELECTION_FILE, {"target": target, "known": pages})


@asynccontextmanager
async def connect(
    name: str, user_data_dir: Path, port: int, *, timeout_ms: float
) -> AsyncIterator[CDPConnection]:
    """Open a browser-level connection to the session browser.

    Raises:
        WebSkrapError: ``session_unreachable`` when nothing answers on the
            session's port.
    """
    path = devtools_path(user_data_dir, port)
    try:
        transport = await WebSocketTransport.connect(
            "127.0.0.1", port, path, timeout_s=min(timeout_ms / 1000, 10.0)
        )
    except (OSError, ConnectionError, TimeoutError) as exc:
        msg = (
            f"session '{name}' is not reachable. "
            "Run: webskrap browser close, then webskrap browser open"
        )
        raise WebSkrapError(msg, code=ErrorCode.SESSION_UNREACHABLE) from exc
    connection = CDPConnection(transport)
    connection.default_timeout_s = timeout_ms / 1000
    connection.start()
    try:
        yield connection
    finally:
        await connection.close()


@asynccontextmanager
async def attach(
    name: str, user_data_dir: Path, port: int, *, timeout_ms: float
) -> AsyncIterator[CDPPage]:
    """Attach to the session browser's current page and detach on exit.

    Uses :func:`current_page`, and creates a blank page when none is open.
    The browser keeps running.

    Raises:
        WebSkrapError: ``session_unreachable`` when nothing answers on the
            session's port.
    """
    async with connect(name, user_data_dir, port, timeout_ms=timeout_ms) as connection:
        order_file = user_data_dir.parent / PAGE_ORDER_FILE
        # Discovery first reports every existing target in no set order, so
        # creations count only from after its reply: pages this command opens
        # (popups) are then recorded in the order Chromium created them.
        created: list[str] = []

        def on_created(params: dict[str, Any]) -> None:
            info = params.get("targetInfo", {})
            if info.get("type") == "page":
                created.append(info["targetId"])

        connection.on("Target.targetCreated", on_created)
        await connection.send("Target.setDiscoverTargets", {"discover": True})
        created.clear()
        targets = await connection.send("Target.getTargets")
        pages = _ordered_pages(
            order_file,
            [info["targetId"] for info in targets["targetInfos"] if info["type"] == "page"],
        )
        target_id = current_page(user_data_dir.parent, pages)
        if target_id is None:
            target_id = (await connection.send("Target.createTarget", {"url": "about:blank"}))[
                "targetId"
            ]
        attached = await connection.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        page = CDPPage(connection, attached["sessionId"], target_id)
        page.default_timeout_s = timeout_ms / 1000
        try:
            yield page
        finally:
            if created:
                _ordered_pages(order_file, [*pages, *created])
            with suppress(WebSkrapError):
                await connection.send(
                    "Target.detachFromTarget", {"sessionId": attached["sessionId"]}, timeout_s=2
                )


async def _tabs(connection: CDPConnection, directory: Path) -> list[dict[str, Any]]:
    infos = (await connection.send("Target.getTargets"))["targetInfos"]
    pages = {info["targetId"]: info for info in infos if info["type"] == "page"}
    return [pages[target] for target in _ordered_pages(directory / PAGE_ORDER_FILE, list(pages))]


def _listing(directory: Path, tabs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current = current_page(directory, [tab["targetId"] for tab in tabs])
    return [
        {
            "index": index,
            "url": tab.get("url", ""),
            "title": tab.get("title", ""),
            "current": tab["targetId"] == current,
        }
        for index, tab in enumerate(tabs)
    ]


def _tab_at(tabs: list[dict[str, Any]], index: int) -> str:
    if not 0 <= index < len(tabs):
        msg = f"no tab {index}: the session has {len(tabs)}. Run: webskrap browser tab-list"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    return str(tabs[index]["targetId"])


async def list_tabs(connection: CDPConnection, directory: Path) -> list[dict[str, Any]]:
    """Return ``[{"index", "url", "title", "current"}]`` in creation order."""
    return _listing(directory, await _tabs(connection, directory))


async def new_tab(connection: CDPConnection, directory: Path) -> list[dict[str, Any]]:
    """Open a blank tab, make it current, and return the tab list."""
    # Record the open tabs first, so the new one sorts last even when no
    # command has written the order yet.
    await _tabs(connection, directory)
    created = await connection.send("Target.createTarget", {"url": "about:blank"})
    tabs = await _tabs(connection, directory)
    _select(directory, created["targetId"], [tab["targetId"] for tab in tabs])
    return _listing(directory, tabs)


async def select_tab(
    connection: CDPConnection, directory: Path, index: int
) -> list[dict[str, Any]]:
    """Make tab ``index`` current and bring it to the front.

    Raises:
        WebSkrapError: ``usage`` when no tab has that index.
    """
    tabs = await _tabs(connection, directory)
    target = _tab_at(tabs, index)
    await connection.send("Target.activateTarget", {"targetId": target})
    _select(directory, target, [tab["targetId"] for tab in tabs])
    return _listing(directory, tabs)


async def close_tab(
    connection: CDPConnection, directory: Path, index: int | None
) -> list[dict[str, Any]]:
    """Close tab ``index``, or the current tab, and return the tabs left.

    Closing the current tab makes the newest remaining tab current.

    Raises:
        WebSkrapError: ``usage`` when no tab has that index.
    """
    tabs = await _tabs(connection, directory)
    if index is None:
        target = current_page(directory, [tab["targetId"] for tab in tabs])
        if target is None:
            return []
    else:
        target = _tab_at(tabs, index)
    await connection.send("Target.closeTarget", {"targetId": target})
    # The target can stay listed for a moment after closeTarget returns.
    left = [tab for tab in tabs if tab["targetId"] != target]
    _ordered_pages(directory / PAGE_ORDER_FILE, [tab["targetId"] for tab in left])
    return _listing(directory, left)


async def page_state(page: CDPPage) -> dict[str, Any]:
    """Return the page's current ``{"url", "title"}``."""
    state = await page.evaluate(_STATE_SCRIPT)
    return {"url": state["url"], "title": state["title"]}


async def goto(
    page: CDPPage, url: str, wait_until: str, *, decline_cookies: bool = False
) -> dict[str, Any]:
    """Navigate and return ``{"status", "url", "title", "blocked"}``.

    Uses the fetch tools' challenge and block detection, including the wait
    for a self-clearing interstitial. A detected page is reported in
    ``blocked`` rather than raised, so the session stays usable for a person
    to look at. With ``decline_cookies``, a consent notice is dismissed and
    ``cookie_notice_declined`` names the strategy that clicked, or is None.
    """
    url = validate_url(url)
    timeout_s = page.default_timeout_s
    deadline = time.monotonic() + timeout_s
    documents = challenges.record_documents(page)
    try:
        response = await page.goto(url, wait_until=wait_until, timeout_s=timeout_s)
        response = await challenges.settle(
            page, documents, response, wait_until=wait_until, deadline=deadline
        )
    finally:
        documents.stop()
    status = challenges.response_status(response)
    found = await challenges.detect(page, challenges.response_headers(response), status)
    result: dict[str, Any] = {"status": status}
    if decline_cookies:
        result["cookie_notice_declined"] = (
            None if found else await consent.decline_cookies(page, timeout_ms=DECLINE_TIMEOUT_MS)
        )
    return {**result, **await page_state(page), "blocked": found.payload() if found else None}


async def history(page: CDPPage, method: str) -> dict[str, Any]:
    """Run ``go_back``, ``go_forward`` or ``reload`` and wait for the load.

    Like Playwright, going past either end of the history does nothing. The
    session browser runs without the back/forward cache, so a move to another
    document always fires a load event; a move between entries of the same
    document (``#anchor`` links, ``pushState``) is done once Chromium reports
    it, since no load follows.

    Raises:
        WebSkrapError: ``timeout`` when neither signal arrives in time.
    """
    await page.enable_events()
    events = page.record("Page.lifecycleEvent", "Page.navigatedWithinDocument")
    try:
        if method == "reload":
            await page.send("Page.reload")
        else:
            entries = await page.send("Page.getNavigationHistory")
            index = entries["currentIndex"] + (-1 if method == "go_back" else 1)
            if not 0 <= index < len(entries["entries"]):
                return await page_state(page)
            await page.send(
                "Page.navigateToHistoryEntry", {"entryId": entries["entries"][index]["id"]}
            )
        # Iframes report lifecycle and in-document navigations too; only the
        # main frame (whose id is the target id) ends the command.
        await events.wait(
            lambda event, params: (
                params.get("frameId") == page.target_id
                and (
                    event == "Page.navigatedWithinDocument"
                    or (event == "Page.lifecycleEvent" and params.get("name") == "load")
                )
            ),
            timeout_s=page.default_timeout_s,
            what=f"{method} to finish",
        )
    finally:
        events.stop()
    return await page_state(page)


async def read_text(page: CDPPage, *, max_chars: int, offset: int = 0) -> dict[str, Any]:
    """Return a visible-text window; see :func:`webskrap.browser_session.read_text`."""
    window = await page.evaluate(
        _TEXT_SCRIPT, {"limit": max(0, max_chars), "start": max(0, offset)}
    )
    state = await page_state(page)
    return {"url": state["url"], **window}


async def evaluate(page: CDPPage, expression: str) -> Any:
    """Evaluate ``expression`` in the page's own world, as Patchright's eval does.

    ``Runtime.evaluate`` without a context id runs in the main world and does
    not need ``Runtime.enable``. A function value is then called with no
    argument, matching Playwright, and objects are serialized as Patchright
    does (see :data:`_SERIALIZE_FUNCTION`). Nothing is wrapped around the
    caller's code itself.

    Raises:
        CDPError: If the expression throws or its value cannot be serialized.
    """
    result = await page.send(
        "Runtime.evaluate",
        # Playwright evaluates with a user gesture, so eval can open a popup or
        # start media the way a page action would.
        {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": False,
            "userGesture": True,
        },
    )
    _raise_for_exception(result)
    remote = result["result"]
    object_id = remote.get("objectId")
    if object_id is None:
        unserializable = remote.get("unserializableValue")
        if unserializable is not None:
            if unserializable.endswith("n"):
                return int(unserializable[:-1])
            return _UNSERIALIZABLE.get(unserializable)
        return remote.get("value")
    try:
        value = await page.send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": _SERIALIZE_FUNCTION,
                "objectId": object_id,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
        )
        _raise_for_exception(value)
        return value["result"].get("value")
    finally:
        with suppress(WebSkrapError):
            await page.send("Runtime.releaseObject", {"objectId": object_id})


def _raise_for_exception(result: dict[str, Any]) -> None:
    if "exceptionDetails" in result:
        details = result["exceptionDetails"]
        description = details.get("exception", {}).get("description") or details.get("text")
        raise CDPError(f"page script failed: {description}")


async def viewport(page: CDPPage) -> Viewport:
    """Return viewport size and scroll offset in CSS pixels."""
    geometry = await page.evaluate(_VIEWPORT_SCRIPT)
    return {
        "width": int(geometry["width"]),
        "height": int(geometry["height"]),
        "scroll_x": float(geometry["scroll_x"]),
        "scroll_y": float(geometry["scroll_y"]),
    }


async def view(page: CDPPage) -> BrowserView:
    """Capture the viewport at one image pixel per CSS pixel.

    On a high-density screen the capture is scaled down by the device pixel
    ratio, so mouse coordinates read off the image are CSS pixels.
    """
    from webskrap.browser_session import BrowserView

    geometry = await page.evaluate(_VIEWPORT_SCRIPT)
    params: dict[str, Any] = {"format": "png"}
    if geometry["dpr"] != 1:
        params["clip"] = {
            "x": geometry["scroll_x"],
            "y": geometry["scroll_y"],
            "width": geometry["width"],
            "height": geometry["height"],
            "scale": 1 / geometry["dpr"],
        }
    shot = await page.send("Page.captureScreenshot", params)
    image = base64.b64decode(shot["data"])
    width, height = struct.unpack(">II", image[16:24])
    state = await page_state(page)
    return BrowserView(
        image=image,
        url=state["url"],
        title=state["title"],
        width=width,
        height=height,
        scroll_x=float(geometry["scroll_x"]),
        scroll_y=float(geometry["scroll_y"]),
    )


async def screenshot(
    page: CDPPage, path: Path, *, full_page: bool, target: str | None = None
) -> dict[str, float] | None:
    """Write a PNG of the viewport, the whole page, or one element to ``path``.

    Returns the element's clip when ``target`` is set, else None.
    """
    clip = await cdp_dom.element_clip(page, target) if target is not None else None
    image = await page.screenshot(full_page=full_page, clip=clip)
    await asyncio.to_thread(path.write_bytes, image)
    return clip


#: Non-character keys: CDP ``key``, ``code``, Windows virtual key code, text.
_SPECIAL_KEYS: dict[str, tuple[str, str, int, str]] = {
    "Enter": ("Enter", "Enter", 13, "\r"),
    "Tab": ("Tab", "Tab", 9, ""),
    "Escape": ("Escape", "Escape", 27, ""),
    "Backspace": ("Backspace", "Backspace", 8, ""),
    "Delete": ("Delete", "Delete", 46, ""),
    "Insert": ("Insert", "Insert", 45, ""),
    "Space": (" ", "Space", 32, " "),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37, ""),
    "ArrowUp": ("ArrowUp", "ArrowUp", 38, ""),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39, ""),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40, ""),
    "Home": ("Home", "Home", 36, ""),
    "End": ("End", "End", 35, ""),
    "PageUp": ("PageUp", "PageUp", 33, ""),
    "PageDown": ("PageDown", "PageDown", 34, ""),
    **{f"F{n}": (f"F{n}", f"F{n}", 111 + n, "") for n in range(1, 13)},
}
#: US layout keys with a shifted character: unshifted, shifted, ``code``, key code.
_US_SHIFT_PAIRS: tuple[tuple[str, str, str, int], ...] = (
    ("`", "~", "Backquote", 192),
    ("1", "!", "Digit1", 49),
    ("2", "@", "Digit2", 50),
    ("3", "#", "Digit3", 51),
    ("4", "$", "Digit4", 52),
    ("5", "%", "Digit5", 53),
    ("6", "^", "Digit6", 54),
    ("7", "&", "Digit7", 55),
    ("8", "*", "Digit8", 56),
    ("9", "(", "Digit9", 57),
    ("0", ")", "Digit0", 48),
    ("-", "_", "Minus", 189),
    ("=", "+", "Equal", 187),
    ("[", "{", "BracketLeft", 219),
    ("]", "}", "BracketRight", 221),
    ("\\", "|", "Backslash", 220),
    (";", ":", "Semicolon", 186),
    ("'", '"', "Quote", 222),
    (",", "<", "Comma", 188),
    (".", ">", "Period", 190),
    ("/", "?", "Slash", 191),
)
#: Character: (``code``, key code, the character Shift turns it into).
_US_KEYS: dict[str, tuple[str, int, str]] = {
    **{low: (code, key_code, high) for low, high, code, key_code in _US_SHIFT_PAIRS},
    **{high: (code, key_code, high) for _low, high, code, key_code in _US_SHIFT_PAIRS},
    **{
        letter: (f"Key{letter.upper()}", ord(letter.upper()), letter.upper())
        for letter in "abcdefghijklmnopqrstuvwxyz"
    },
    **{letter: (f"Key{letter}", ord(letter), letter) for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
}
#: Modifier name: (CDP modifier bit, key code, ``code``).
_MODIFIERS: dict[str, tuple[int, int, str]] = {
    "Alt": (1, 18, "AltLeft"),
    "Control": (2, 17, "ControlLeft"),
    "Meta": (4, 91, "MetaLeft"),
    "Shift": (8, 16, "ShiftLeft"),
}


def key_events(combo: str) -> list[dict[str, Any]]:
    """Return the ``Input.dispatchKeyEvent`` calls for a Playwright key combo.

    ``combo`` is Playwright's syntax: modifiers and one key joined by ``+``,
    such as ``Enter``, ``Shift+Tab`` or ``Control+a``. Modifiers go down in
    order and up in reverse around the key. A key with Control, Alt or Meta
    held inserts no text, as on a real keyboard.

    Raises:
        WebSkrapError: ``usage`` for an unknown key name.
    """
    # "+" alone and a trailing "++" (Shift++) name the plus key itself.
    if combo == "+":
        modifier_names, key_name = [], "+"
    elif combo.endswith("++"):
        modifier_names, key_name = combo[:-2].split("+"), "+"
    else:
        *modifier_names, key_name = combo.split("+")
    for modifier in modifier_names:
        if modifier not in _MODIFIERS:
            msg = f"unknown modifier '{modifier}' in key '{combo}'"
            raise WebSkrapError(msg, code=ErrorCode.USAGE)
    if key_name in _SPECIAL_KEYS:
        key, code, key_code, text = _SPECIAL_KEYS[key_name]
    elif len(key_name) == 1:
        key, text = key_name, key_name
        code, key_code, _shifted = _US_KEYS.get(key_name, ("", 0, key_name))
    else:
        msg = f"unknown key '{key_name}': use a single character or a name like Enter"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    mask = 0
    events: list[dict[str, Any]] = []
    for modifier in modifier_names:
        bit, modifier_code, modifier_physical = _MODIFIERS[modifier]
        mask |= bit
        events.append(
            {
                "type": "rawKeyDown",
                "key": modifier,
                "code": modifier_physical,
                "windowsVirtualKeyCode": modifier_code,
                "modifiers": mask,
            }
        )
    if mask & 8 and key in _US_KEYS:
        # Shift turns the key into its US-layout partner: a to A, 1 to !.
        key = text = _US_KEYS[key][2]
    if mask & (1 | 2 | 4):
        text = ""
    down = {
        "type": "keyDown" if text else "rawKeyDown",
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": key_code,
        "modifiers": mask,
    }
    if text:
        down["text"] = down["unmodifiedText"] = text
    events.append(down)
    up = {name: value for name, value in down.items() if name not in ("text", "unmodifiedText")}
    events.append({**up, "type": "keyUp"})
    for modifier in reversed(modifier_names):
        bit, modifier_code, modifier_physical = _MODIFIERS[modifier]
        mask &= ~bit
        events.append(
            {
                "type": "keyUp",
                "key": modifier,
                "code": modifier_physical,
                "windowsVirtualKeyCode": modifier_code,
                "modifiers": mask,
            }
        )
    return events


async def press(page: CDPPage, combo: str) -> None:
    """Press a key combo such as ``Enter`` or ``Control+a``; see :func:`key_events`."""
    for event in key_events(combo):
        await page.send("Input.dispatchKeyEvent", event)


async def insert_text(page: CDPPage, text: str) -> dict[str, Any]:
    """Insert literal text into the focused control without key events."""
    await page.send("Input.insertText", {"text": text})
    return await page_state(page)


_BUTTON_BITS = {"left": 1, "right": 2, "middle": 4}


async def _mouse(
    page: CDPPage, kind: str, x: float, y: float, *, button: str = "none", **extra: Any
) -> None:
    await page.send(
        "Input.dispatchMouseEvent",
        {"type": kind, "x": x, "y": y, "button": button, **extra},
    )


async def mouse_action(
    page: CDPPage,
    action: str,
    x: float,
    y: float,
    *,
    end_x: float | None = None,
    end_y: float | None = None,
    delta_x: float = 0,
    delta_y: float = 0,
    button: str = "left",
) -> dict[str, Any]:
    """Send trusted mouse input; arguments match :func:`browser_session.mouse_action`.

    The caller has validated the arguments and the viewport bounds.
    """
    pressed = _BUTTON_BITS.get(button, 1)
    if action in {"click", "dblclick"}:
        await _mouse(page, "mouseMoved", x, y)
        for click_count in (1, 2) if action == "dblclick" else (1,):
            await _mouse(
                page,
                "mousePressed",
                x,
                y,
                button=button,
                buttons=pressed,
                clickCount=click_count,
            )
            await _mouse(page, "mouseReleased", x, y, button=button, clickCount=click_count)
    elif action == "move":
        await _mouse(page, "mouseMoved", x, y)
    elif action == "scroll":
        await _mouse(page, "mouseMoved", x, y)
        await _mouse(page, "mouseWheel", x, y, deltaX=delta_x, deltaY=delta_y)
    elif action == "drag" and end_x is not None and end_y is not None:
        await _drag(page, x, y, end_x, end_y, button=button, pressed=pressed)
    return await page_state(page)


async def _drag(
    page: CDPPage, x: float, y: float, end_x: float, end_y: float, *, button: str, pressed: int
) -> None:
    """Drag in ten steps, as Playwright's ``mouse.move(steps=10)`` does.

    Starting on a selection, a link or a draggable element begins a native
    drag, after which Chromium swallows raw mouse events, so the page would
    never see the release. As in Playwright, drags are intercepted while the
    button is down: once Chromium reports one, the rest of the gesture is sent
    as ``dragEnter``/``dragOver``/``drop`` events with the drag's data.
    """
    await _mouse(page, "mouseMoved", x, y)
    await _mouse(page, "mousePressed", x, y, button=button, buttons=pressed, clickCount=1)
    intercepted = page.record("Input.dragIntercepted")
    await page.send("Input.setInterceptDrags", {"enabled": True})
    data: dict[str, Any] | None = None
    try:
        for step in range(1, 11):
            px = x + (end_x - x) * step / 10
            py = y + (end_y - y) * step / 10
            if data is not None:
                await _drag_event(page, "dragOver", px, py, data)
                continue
            await _mouse(page, "mouseMoved", px, py, button=button, buttons=pressed)
            # The interception event, when any, arrives before the move's reply.
            if intercepted.events:
                started: dict[str, Any] = intercepted.events[-1][1]["data"]
                data = started
                await _drag_event(page, "dragEnter", px, py, started)
    finally:
        intercepted.stop()
        if data is not None:
            await _drag_event(page, "drop", end_x, end_y, data)
        else:
            await _mouse(page, "mouseReleased", end_x, end_y, button=button, clickCount=1)
        await page.send("Input.setInterceptDrags", {"enabled": False})


async def _drag_event(page: CDPPage, kind: str, x: float, y: float, data: dict[str, Any]) -> None:
    await page.send("Input.dispatchDragEvent", {"type": kind, "x": x, "y": y, "data": data})


async def _within(awaitable: Awaitable[T], deadline: float) -> T:
    """Await ``awaitable`` but give up at ``deadline``, raising ``TimeoutError``.

    A busy renderer can stall one evaluation for its whole command timeout;
    bounding each step keeps a wait inside the time the caller asked for.
    """
    return await asyncio.wait_for(awaitable, max(0.0, deadline - time.monotonic()))


async def _poll(check: Callable[[], Awaitable[bool]], timeout_ms: float, what: str) -> None:
    """Poll the async predicate ``check`` until it is true or time runs out."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        # A navigation can destroy the isolated world mid-call; poll again.
        with suppress(CDPError, TimeoutError):
            if await _within(check(), deadline):
                return
        if time.monotonic() >= deadline:
            msg = f"timed out after {timeout_ms:.0f}ms waiting for {what}"
            raise WebSkrapError(msg, code=ErrorCode.TIMEOUT)
        await asyncio.sleep(_POLL_S)


async def wait_for(
    page: CDPPage,
    *,
    condition: str,
    text: str | None = None,
    text_gone: str | None = None,
    selector: str | None = None,
    selector_state: ElementState = "visible",
    load_state: LoadState | None = None,
    timeout_ms: float,
) -> dict[str, Any]:
    """Wait for one condition; see :func:`webskrap.browser_session.wait_for`.

    ``selector`` is a snapshot ref or a CSS selector on this driver.

    Raises:
        WebSkrapError: ``timeout`` when the condition is not met in time, or
            ``usage`` for a selector that is not valid CSS.
    """
    if text is not None:
        needle = text

        async def text_visible() -> bool:
            return bool(await page.evaluate(_TEXT_VISIBLE_SCRIPT, {"needle": needle}))

        await _poll(text_visible, timeout_ms, f"text '{text}'")
    if text_gone is not None:
        gone = text_gone

        async def text_hidden() -> bool:
            return not await page.evaluate(_TEXT_VISIBLE_SCRIPT, {"needle": gone})

        await _poll(text_hidden, timeout_ms, f"text '{text_gone}' to disappear")
    if selector is not None:
        # Imported here: cdp_dom builds on this module's key handling.
        from webskrap.cdp_dom import element_state

        target = selector
        wanted = selector_state

        async def selector_reached() -> bool:
            state = await element_state(page, target)
            if wanted == "attached":
                return state != "detached"
            if wanted == "hidden":
                return state != "visible"
            return state == wanted

        await _poll(selector_reached, timeout_ms, f"{selector} to be {selector_state}")
    if load_state is not None:
        await _wait_load_state(page, load_state, timeout_ms)
    return {**await page_state(page), "matched": condition}


async def _within_or_timeout(awaitable: Awaitable[T], deadline: float, timeout_ms: float) -> T:
    try:
        return await _within(awaitable, deadline)
    except TimeoutError as exc:
        msg = f"timed out after {timeout_ms:.0f}ms waiting for load state networkidle"
        raise WebSkrapError(msg, code=ErrorCode.TIMEOUT) from exc


async def _wait_load_state(page: CDPPage, load_state: LoadState, timeout_ms: float) -> None:
    """Wait until the current document reached ``load_state``, within ``timeout_ms``.

    ``document.readyState`` answers for the two document states. The live
    ``networkIdle`` lifecycle event usually fired before this connection
    existed, so ``networkidle`` is measured here instead: 500 ms with no
    request in flight that this connection saw start, and no new entry in the
    page's resource timeline (which also covers requests this connection
    could not see begin, once they finish). A request that began before the
    command attached and is still running is invisible until it ends; the
    same holds for Patchright, which also attaches fresh for each command.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    in_flight: set[str] = set()
    network = None
    if load_state == "networkidle":
        await page.enable_events()
        network = page.record(
            "Network.requestWillBeSent", "Network.loadingFinished", "Network.loadingFailed"
        )
    try:
        wanted = "interactive" if load_state == "domcontentloaded" else "complete"

        async def ready() -> bool:
            state = await page.evaluate("document.readyState")
            return state == "complete" or state == wanted

        await _poll(ready, max(0.0, deadline - time.monotonic()) * 1000, f"load state {load_state}")
        if network is None:
            return
        count_script = "performance.getEntriesByType('resource').length"
        last = await _within_or_timeout(page.evaluate(count_script), deadline, timeout_ms)
        quiet_since = time.monotonic()
        seen = 0
        while True:
            for method, params in network.events[seen:]:
                request = params.get("requestId", "")
                if method == "Network.requestWillBeSent":
                    in_flight.add(request)
                else:
                    in_flight.discard(request)
            seen = len(network.events)
            current = await _within_or_timeout(page.evaluate(count_script), deadline, timeout_ms)
            if in_flight or current != last:
                last, quiet_since = current, time.monotonic()
            elif time.monotonic() - quiet_since >= _NETWORK_QUIET_S:
                return
            if time.monotonic() >= deadline:
                msg = f"timed out after {timeout_ms:.0f}ms waiting for load state networkidle"
                raise WebSkrapError(msg, code=ErrorCode.TIMEOUT)
            await asyncio.sleep(_POLL_S)
    finally:
        if network is not None:
            network.stop()
