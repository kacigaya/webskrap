"""Attach to Chromium targets and persist tab and pointer selection."""

from __future__ import annotations

import json
import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from webskrap.driver.cdp import CDPConnection, CDPPage
from webskrap.driver.polling import poll
from webskrap.driver.transport import WebSocketTransport
from webskrap.errors import ErrorCode, WebSkrapError

#: How long closing a tab waits for Chromium to stop listing it.
CLOSE_TAB_TIMEOUT_MS = 5_000


#: Session-directory file with page target ids in creation order.
PAGE_ORDER_FILE = "cdp-pages.json"
#: Last mouse position per page target, kept between commands.
POINTER_FILE = "cdp-pointer.json"
#: Session-directory file with the selected tab and the pages known then.
TAB_SELECTION_FILE = "cdp-tab.json"


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


def _ordered_pages(order_file: Path, current: list[str], *, remember: bool = True) -> list[str]:
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
    if remember and ordered != known:
        _write_ids(order_file, ordered)
    return ordered


def _write_ids(path: Path, data: object) -> None:
    temp = path.with_suffix(".tmp")
    # Owner-only like the rest of the session directory; ids only, no data.
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temp, path)


def saved_pointer(path: Path, target: str) -> tuple[float, float] | None:
    """Return the mouse position a previous command left on ``target``.

    Each command connects anew, and Chromium keeps the last position, so the
    next path starts there. None when nothing valid is saved.
    """
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    point = saved.get(target) if isinstance(saved, dict) else None
    if (
        isinstance(point, list)
        and len(point) == 2
        and all(isinstance(value, int | float) and math.isfinite(value) for value in point)
    ):
        return float(point[0]), float(point[1])
    return None


def save_pointer(path: Path, target: str, pointer: tuple[float, float], live: set[str]) -> None:
    """Record ``target``'s mouse position, dropping pages that are gone."""
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    kept = (
        {key: value for key, value in saved.items() if key in live}
        if isinstance(saved, dict)
        else {}
    )
    _write_ids(path, {**kept, target: list(pointer)})


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

    The WebSocket uses its own connection timeout. ``timeout_ms`` sets the
    default reply timeout for CDP commands after the connection opens.

    Raises:
        WebSkrapError: ``session_unreachable`` when nothing answers on the
            session's port.
    """
    path = devtools_path(user_data_dir, port)
    try:
        transport = await WebSocketTransport.connect("127.0.0.1", port, path)
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
    name: str,
    user_data_dir: Path,
    port: int,
    *,
    timeout_ms: float,
    read_only: bool = False,
) -> AsyncIterator[CDPPage]:
    """Attach to the session browser's current page and detach on exit.

    Uses :func:`current_page`. In read-only mode it leaves session files
    intact and requires an existing tab. The browser keeps running.

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

        if not read_only:
            connection.on("Target.targetCreated", on_created)
            await connection.send("Target.setDiscoverTargets", {"discover": True})
            created.clear()
        targets = await connection.send("Target.getTargets")
        pages = _ordered_pages(
            order_file,
            [info["targetId"] for info in targets["targetInfos"] if info["type"] == "page"],
            remember=not read_only,
        )
        target_id = current_page(user_data_dir.parent, pages)
        if target_id is None:
            if read_only:
                raise WebSkrapError("No browser tab is open")
            target_id = (await connection.send("Target.createTarget", {"url": "about:blank"}))[
                "targetId"
            ]
        attached = await connection.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        page = CDPPage(connection, attached["sessionId"], target_id)
        page.default_timeout_s = timeout_ms / 1000
        pointer_file = user_data_dir.parent / POINTER_FILE
        page.pointer = saved_pointer(pointer_file, target_id)
        start = page.pointer
        try:
            yield page
        finally:
            if created:
                _ordered_pages(order_file, [*pages, *created])
            if not read_only and page.pointer is not None and page.pointer != start:
                save_pointer(pointer_file, target_id, page.pointer, {*pages, *created})
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

    # The target can stay listed for a moment after closeTarget returns, and
    # the next command would then pick the closed tab as current and fail.
    async def gone() -> bool:
        listed = await connection.send("Target.getTargets")
        return all(info["targetId"] != target for info in listed["targetInfos"])

    with suppress(WebSkrapError):
        await poll(gone, CLOSE_TAB_TIMEOUT_MS, "the closed tab to go away")
    left = [tab for tab in tabs if tab["targetId"] != target]
    _ordered_pages(directory / PAGE_ORDER_FILE, [tab["targetId"] for tab in left])
    return _listing(directory, left)
