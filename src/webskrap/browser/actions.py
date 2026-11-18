"""Page actions shared by the browser CLI and MCP."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from webskrap.browser import navigation
from webskrap.config.models import (
    ElementState,
    LoadState,
    WaitUntil,
)
from webskrap.driver import cdp_dom, cdp_session, text_search
from webskrap.driver import snapshot as cdp_snapshot
from webskrap.driver.cdp import CDPPage
from webskrap.driver.text_search import text_window
from webskrap.driver.view_types import BrowserView, Viewport
from webskrap.errors import ErrorCode, WebSkrapError

DEFAULT_ACTION_TIMEOUT_MS = 10_000.0

#: Element action name -> value arity: "none", "one" or "many".
ELEMENT_ACTIONS: dict[str, str] = {
    "click": "none",
    "dblclick": "none",
    "hover": "none",
    "check": "none",
    "uncheck": "none",
    "fill": "one",
    "type": "one",
    "select": "many",
}


def element_arguments(action: str, values: list[str]) -> list[Any]:
    """Validate ``values`` against an :data:`ELEMENT_ACTIONS` entry's arity."""
    if action not in ELEMENT_ACTIONS:
        supported = ", ".join(ELEMENT_ACTIONS)
        msg = f"unknown action '{action}': use one of {supported}"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    arity = ELEMENT_ACTIONS[action]
    if arity == "none":
        if values:
            msg = f"'{action}' takes no value argument"
            raise WebSkrapError(msg, code=ErrorCode.USAGE)
        return []
    if arity == "one":
        if len(values) != 1:
            msg = f"'{action}' takes exactly one value argument"
            raise WebSkrapError(msg, code=ErrorCode.USAGE)
        return [values[0]]
    if not values:
        msg = f"'{action}' takes at least one value argument"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    return [values]


async def element_action(page: CDPPage, action: str, target: str, values: list[str]) -> None:
    """Run an :data:`ELEMENT_ACTIONS` interaction against ``target``.

    ``target`` is a snapshot ref (``e12``) or a CSS selector. Clicks go along a
    human cursor path with a human button hold, refusing a covered element;
    ``type`` clicks into the field and types with human keystroke timing;
    ``fill`` sets the value at once. See :func:`webskrap.cdp_dom.element_action`.
    """
    arguments = element_arguments(action, values)
    await cdp_dom.element_action(page, action, target, arguments)


async def evaluate(page: CDPPage, expression: str) -> Any:
    """Evaluate ``expression`` in the page's own JavaScript world.

    An ``eval`` caller expects what the page's scripts see, such as
    ``window.app``, so this does not use an isolated world. See
    :func:`webskrap.cdp_session.evaluate`.
    """
    return await cdp_session.evaluate(page, expression)


async def page_state(page: CDPPage) -> dict[str, Any]:
    """Return the page's current ``{"url", "title"}``."""
    return await cdp_session.page_state(page)


async def viewport(page: CDPPage) -> Viewport:
    """Return viewport size and scroll offset in CSS pixels."""
    return await cdp_session.viewport(page)


async def view(page: CDPPage) -> BrowserView:
    """Capture the visible page at one image pixel per CSS pixel.

    Full-page and device-scale screenshots cannot be used directly with mouse
    coordinates. Read PNG dimensions so metadata describes the actual image.
    """
    return await cdp_session.view(page)


def validate_mouse(
    action: str,
    x: float,
    y: float,
    *,
    end_x: float | None = None,
    end_y: float | None = None,
    delta_x: float = 0,
    delta_y: float = 0,
    button: str = "left",
) -> None:
    """Reject invalid or ignored mouse arguments before connecting or acting."""
    reason = None
    if action not in {"click", "dblclick", "move", "scroll", "drag"}:
        reason = "mouse action must be click, dblclick, move, scroll, or drag"
    elif button not in {"left", "middle", "right"}:
        reason = "button must be left, middle, or right"
    elif action in {"move", "scroll"} and button != "left":
        reason = "button is only used with click, dblclick, or drag"
    elif any(
        not math.isfinite(value)
        for value in (x, y, end_x, end_y, delta_x, delta_y)
        if value is not None
    ):
        reason = "mouse coordinates and deltas must be finite"
    elif x < 0 or y < 0:
        reason = "mouse coordinates must be non-negative"
    elif action == "drag" and (end_x is None or end_y is None):
        reason = "drag requires end_x and end_y"
    elif action != "drag" and (end_x is not None or end_y is not None):
        reason = "end_x and end_y are only used with drag"
    elif action != "scroll" and (delta_x != 0 or delta_y != 0):
        reason = "delta_x and delta_y are only used with scroll"
    elif action == "scroll" and delta_x == 0 and delta_y == 0:
        reason = "scroll requires a non-zero delta_x or delta_y"
    if reason is not None:
        raise WebSkrapError(reason, code=ErrorCode.USAGE)


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
    """Send trusted mouse input at viewport CSS coordinates from a fresh view.

    Scroll targets the element under (x, y), including nested scroll areas.
    Coordinate clicks hit whatever is visible there; prefer element_action
    when a ref or selector identifies the intended element.
    """
    validate_mouse(
        action,
        x,
        y,
        end_x=end_x,
        end_y=end_y,
        delta_x=delta_x,
        delta_y=delta_y,
        button=button,
    )
    geometry = await viewport(page)
    points = [(x, y)]
    if end_x is not None and end_y is not None:
        points.append((end_x, end_y))
    if any(not (0 <= px < geometry["width"] and 0 <= py < geometry["height"]) for px, py in points):
        msg = "mouse coordinates are outside the viewport; take a fresh view before acting"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    return await cdp_session.mouse_action(
        page,
        action,
        x,
        y,
        end_x=end_x,
        end_y=end_y,
        delta_x=delta_x,
        delta_y=delta_y,
        button=button,
    )


async def insert_text(page: CDPPage, text: str) -> dict[str, Any]:
    """Insert literal text into the focused control without interpreting keys."""
    return await cdp_session.insert_text(page, text)


async def goto(
    page: CDPPage,
    url: str,
    wait_until: WaitUntil,
    *,
    decline_cookies: bool = False,
    solve_cloudflare: bool = False,
    solve_datadome: bool = False,
    solve_perimeterx: bool = False,
    solve_recaptcha: bool = False,
) -> dict[str, Any]:
    """Navigate ``page`` to ``url`` and return its status and state.

    ``blocked`` reports a recognized challenge or block page (or None)
    without raising. With ``solve_cloudflare``, Cloudflare's Turnstile
    checkbox is clicked first; with ``solve_datadome``, DataDome's slider is
    dragged; with ``solve_perimeterx``, HUMAN's Press & Hold button is held;
    with ``solve_recaptcha``, a reCAPTCHA v2 or hCaptcha checkbox is clicked.
    With ``decline_cookies``, a consent notice is
    dismissed and ``cookie_notice_declined`` reports the strategy, as fetch
    does.
    """
    return await navigation.goto(
        page,
        url,
        wait_until,
        decline_cookies=decline_cookies,
        solve_cloudflare=solve_cloudflare,
        solve_datadome=solve_datadome,
        solve_perimeterx=solve_perimeterx,
        solve_recaptcha=solve_recaptcha,
    )


#: History commands :func:`history` accepts, named after the 2.x Playwright methods.
HISTORY_METHODS = ("go_back", "go_forward", "reload")


async def history(page: CDPPage, method: str) -> dict[str, Any]:
    """Run a :data:`HISTORY_METHODS` command and return the page state after it."""
    if method not in HISTORY_METHODS:
        msg = f"history method must be one of {', '.join(HISTORY_METHODS)}"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    return await cdp_session.history(page, method)


async def solve_captcha(page: CDPPage) -> dict[str, Any]:
    """Click the reCAPTCHA v2 or hCaptcha checkbox on ``page``; return the state.

    ``captcha`` is :func:`webskrap.recaptcha.solve`'s ``{"provider", "state"}``.
    """
    from webskrap.challenges import recaptcha

    result = await recaptcha.solve(page)
    return {**await page_state(page), "captcha": result}


async def press(page: CDPPage, key: str) -> dict[str, Any]:
    """Press ``key`` (e.g. ``Enter`` or ``Control+a``); return the page state."""
    await cdp_session.press(page, key)
    return await page_state(page)


async def screenshot(
    page: CDPPage, path: Path, *, full_page: bool, target: str | None = None
) -> dict[str, Any]:
    """Write a PNG to ``path``; return the page state plus ``path``.

    ``target`` (a CSS selector or snapshot ref, iframe refs included)
    captures only that element's bounding box, reported as ``clip``.

    Raises:
        WebSkrapError: ``usage`` when ``target`` is combined with ``full_page``.
    """
    if target is not None and not target.strip():
        raise WebSkrapError("target cannot be blank", code=ErrorCode.USAGE)
    if target is not None and full_page:
        raise WebSkrapError("pass either a target or full_page, not both", code=ErrorCode.USAGE)
    clip = await cdp_session.screenshot(page, path, full_page=full_page, target=target)
    result = {**await page_state(page), "path": str(path)}
    if clip is not None:
        result["clip"] = clip
    return result


async def snapshot(
    page: CDPPage,
    *,
    depth: int | None = None,
    target: str | None = None,
    boxes: bool = False,
) -> dict[str, Any]:
    """Return the page state plus a snapshot whose elements carry ``eN`` refs."""
    tree = await cdp_snapshot.snapshot(page, depth=depth, target=target, boxes=boxes)
    return {**await page_state(page), "snapshot": tree}


async def read_text(page: CDPPage, *, max_chars: int, offset: int = 0) -> dict[str, Any]:
    """Return a visible-text window, limiting data transferred from the browser.

    Offsets count Unicode code points, to match Python's text_window offsets,
    including astral characters. The browser still computes the full body
    text to report its exact length.
    """
    return await cdp_session.read_text(page, max_chars=max_chars, offset=offset)


#: Page text searched by :func:`search_text`; longer text is cut, not refused.
MAX_SEARCH_TEXT_CHARS = 2_000_000


async def search_text(
    page: CDPPage, pattern: str, *, context: int = 1, max_matches: int = 20
) -> dict[str, Any]:
    """Return the lines of the page's visible text that ``pattern`` matches.

    The text is extracted once and searched in Python, never in the page; see
    :mod:`webskrap.text_search` for the caps and the time limit. Only the
    first :data:`MAX_SEARCH_TEXT_CHARS` characters are searched, and
    ``text_searched_truncated`` says when the page had more.
    """
    text_search.validate_search(pattern, context=context, max_matches=max_matches)
    state = await cdp_session.body_text(page, max_chars=MAX_SEARCH_TEXT_CHARS)
    found = await text_search.search_lines(
        state["text"], pattern, context=context, max_matches=max_matches
    )
    return {
        "url": state["url"],
        "title": state["title"],
        "pattern": pattern,
        **found,
        "text_length": state["text_length"],
        "text_searched_truncated": state["text_length"] > MAX_SEARCH_TEXT_CHARS,
    }


def wait_condition(
    text: str | None,
    text_gone: str | None,
    selector: str | None,
    load_state: LoadState | None,
) -> str:
    """Return the name of the single condition :func:`wait_for` should wait on.

    Raises:
        WebSkrapError: If the caller asked for no condition or several. Waiting
            on two things at once has no obvious meaning -- neither "both" nor
            "either" is what a caller who passed two by accident wanted.
    """
    requested = [
        name
        for name, value in (
            ("text", text),
            ("text_gone", text_gone),
            ("selector", selector),
            ("load_state", load_state),
        )
        if value is not None
    ]
    if len(requested) != 1:
        msg = (
            "wait takes exactly one of text, text_gone, selector or load_state; "
            f"got {len(requested)}"
        )
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    return requested[0]


async def wait_for(
    page: CDPPage,
    *,
    text: str | None = None,
    text_gone: str | None = None,
    selector: str | None = None,
    selector_state: ElementState = "visible",
    load_state: LoadState | None = None,
    timeout_ms: float = DEFAULT_ACTION_TIMEOUT_MS,
) -> dict[str, Any]:
    """Wait for one page condition, then return the page state that followed.

    Without this, waiting after a click means polling with ``eval`` or hoping a
    fixed timeout is long enough. Exactly one condition is accepted; see
    :func:`wait_condition`.

    Args:
        page: Page to wait on.
        text: Wait until this text is visible.
        text_gone: Wait until this text is no longer visible. Text that was
            never there satisfies this immediately.
        selector: Wait until this snapshot ref or CSS selector reaches
            ``selector_state``.
        selector_state: Element state to wait for, with ``selector``.
        load_state: Wait until the page reaches this load state.
        timeout_ms: How long to wait before giving up.

    Returns:
        ``{"url", "title", "matched"}``, where ``matched`` names the condition.
    """
    # wait_condition has already established that exactly one of these is set,
    # so these run as a chain of one; separate ifs keep each branch's argument
    # narrowed to a non-None type without an assert or a cast.
    condition = wait_condition(text, text_gone, selector, load_state)
    return await cdp_session.wait_for(
        page,
        condition=condition,
        text=text,
        text_gone=text_gone,
        selector=selector,
        selector_state=selector_state,
        load_state=load_state,
        timeout_ms=timeout_ms,
    )


def shape_snapshot(result: dict[str, Any], max_chars: int, offset: int = 0) -> dict[str, Any]:
    """Window a :func:`snapshot` payload's tree to ``max_chars`` from ``offset``.

    An aria snapshot of a real page runs well past what a caller wants in one
    piece, so the tree is paged the same way fetched text is: the reported
    ``next_snapshot_offset`` is what to pass back for the rest, and None means
    the whole tree has been read.
    """
    window = text_window(result["snapshot"], max_chars, offset)
    return {
        **result,
        "snapshot": window.text,
        "snapshot_length": window.length,
        "snapshot_offset": window.offset,
        "snapshot_truncated": window.truncated,
        "next_snapshot_offset": window.next_offset,
    }


def shape_eval_result(value: Any, max_chars: int) -> dict[str, Any]:
    """Bound an ``eval`` result so one careless expression cannot flood a caller.

    ``document.body.innerHTML`` is one keystroke away from ``document.title``,
    and the first can be megabytes. The value is returned as-is while its JSON
    encoding fits ``max_chars``; past that ``result`` is None and the clipped
    encoding is returned as ``result_json`` instead, so a truncated payload is
    never mistaken for valid JSON. ``result_truncated`` is what tells a None
    result apart from an expression that genuinely evaluated to null.
    """
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    limit = max(0, max_chars)
    if len(encoded) <= limit:
        return {"result": value, "result_length": len(encoded), "result_truncated": False}
    return {
        "result": None,
        "result_json": encoded[:limit],
        "result_length": len(encoded),
        "result_truncated": True,
    }
