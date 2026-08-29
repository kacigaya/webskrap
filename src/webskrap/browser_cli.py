"""Typer commands for persistent browser sessions (`webskrap browser ...`).

A native Python take on the official Playwright CLI (`@playwright/cli`):
`open` launches a detached Chromium that outlives each CLI invocation, and
every other command reconnects to it over CDP, acts on the current page, and
exits. The session/page logic lives in :mod:`webskrap.browser_session`, shared
with the MCP server; this module only parses arguments and formats output.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn, TypeVar
from uuid import uuid4

import typer
from playwright.async_api import Page
from rich.console import Console
from rich.table import Table

from webskrap import browser_session
from webskrap.browser_session import (
    DEFAULT_ACTION_TIMEOUT_MS,
    DEFAULT_NAVIGATION_TIMEOUT_MS,
    ELEMENT_ACTIONS,
)
from webskrap.client import WebSkrapError
from webskrap.models import ElementState, LoadState, WaitUntil
from webskrap.parsing import parse_element_state, parse_load_state, parse_wait_until

browser_app = typer.Typer(
    help="Drive a persistent browser with Playwright CLI-style commands.",
    no_args_is_help=True,
)
console = Console()
stderr_console = Console(stderr=True, highlight=False)

T = TypeVar("T")
OutputFormat = Literal["human", "json"]

SessionOption = Annotated[str, typer.Option("--session", "-s", help="Browser session name.")]
FormatOption = Annotated[str, typer.Option("--format", help="Output format: human or json.")]
ActionTimeoutOption = Annotated[float, typer.Option("--timeout-ms", min=1, help="Action timeout.")]

_ELEMENT_COMMAND_HELP = {
    "click": "Click an element.",
    "dblclick": "Double-click an element.",
    "hover": "Hover over an element.",
    "check": "Check a checkbox or radio.",
    "uncheck": "Uncheck a checkbox.",
    "fill": "Fill an input with a value.",
    "type": "Type text into an element key by key.",
    "select": "Select option value(s) in a <select>.",
}


def _fail(message: str) -> NoReturn:
    stderr_console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


def _parse_output_format(value: str) -> OutputFormat:
    if value not in ("human", "json"):
        raise typer.BadParameter("must be one of: human, json")
    return value


def _parse_wait_until(value: str) -> WaitUntil:
    try:
        return parse_wait_until(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc).partition(" must ")[2]) from exc


def _parse_load_state(value: str) -> LoadState:
    try:
        return parse_load_state(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc).partition(" must ")[2]) from exc


def _parse_element_state(value: str) -> ElementState:
    try:
        return parse_element_state(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc).partition(" must ")[2]) from exc


def _print_json(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False))


def _run(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a browser-session coroutine, converting failures to CLI errors."""
    try:
        return asyncio.run(coroutine)
    except (typer.Exit, typer.Abort, typer.BadParameter):
        raise
    except Exception as exc:
        # One-line error and exit code 1 for every failure, Playwright or
        # otherwise (e.g. an unwritable screenshot path).
        _fail(str(exc).strip().splitlines()[0] or type(exc).__name__)


def _run_page_command(
    session: str,
    action: Callable[[Page], Awaitable[T]],
    *,
    timeout_ms: float = DEFAULT_ACTION_TIMEOUT_MS,
) -> T:
    return _run(browser_session.run_page_action(session, action, timeout_ms=timeout_ms))


def _emit_state(state: dict[str, Any], output_format: OutputFormat) -> None:
    if output_format == "json":
        _print_json(state)
        return
    if (status := state.get("status")) is not None:
        console.print(f"[bold]Status:[/bold] {status}")
    console.print(f"[bold]URL:[/bold] {state['url']}")
    console.print(f"[bold]Title:[/bold] {state['title']}")


@browser_app.command("open")
def open_command(
    url: Annotated[str | None, typer.Argument(help="URL to open after launch.")] = None,
    session: SessionOption = "default",
    headed: Annotated[
        bool, typer.Option("--headed", help="Run the browser with a visible window.")
    ] = False,
    no_sandbox: Annotated[
        bool,
        typer.Option(
            "--no-sandbox",
            help="Disable Chromium's OS sandbox (weakens isolation; only where it cannot start).",
        ),
    ] = False,
    format: FormatOption = "human",
) -> None:
    """Start (or reuse) a persistent browser session.

    The browser keeps Chromium's OS sandbox unless --no-sandbox is passed (or
    WEBSKRAP_CHROMIUM_SANDBOX=0 is set for hosts that cannot sandbox at all).
    Without it, a renderer compromised by a hostile page is no longer contained.
    """
    output_format = _parse_output_format(format)
    payload = _run(
        browser_session.open_session(
            session,
            headless=not headed,
            # None, not True: an unset flag leaves the environment default in
            # place, while --no-sandbox is an explicit opt-out that wins.
            chromium_sandbox=False if no_sandbox else None,
        )
    )
    if url:
        payload.update(
            _run_page_command(
                session,
                lambda page: browser_session.goto(page, url, "load"),
                timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS,
            )
        )

    if output_format == "json":
        _print_json(payload)
        return
    verb = "Reusing" if payload["reused"] else "Opened"
    console.print(f"{verb} session [bold]{session}[/bold] (pid {payload['pid']})")
    if not payload["chromium_sandbox"]:
        stderr_console.print("[yellow]warning:[/yellow] Chromium sandbox disabled")
    if url:
        _emit_state(payload, output_format)


@browser_app.command("close")
def close_command(
    session: SessionOption = "default",
    all_sessions: Annotated[bool, typer.Option("--all", help="Close every session.")] = False,
    delete_data: Annotated[
        bool, typer.Option("--delete-data", help="Also delete the session's profile data.")
    ] = False,
    format: FormatOption = "human",
) -> None:
    """Close a browser session (its profile data persists unless --delete-data)."""
    output_format = _parse_output_format(format)
    try:
        if all_sessions:
            names = browser_session.list_session_names()
        else:
            directory = browser_session.session_dir(session)
            if browser_session.read_state(directory) is None and not delete_data:
                _fail(f"session '{session}' is not open")
            names = [session]
        closed = [browser_session.close_session(name, delete_data=delete_data) for name in names]
    except WebSkrapError as exc:
        _fail(str(exc))
    if output_format == "json":
        _print_json({"closed": closed})
        return
    for entry in closed:
        console.print(f"closed [bold]{entry['session']}[/bold]")


@browser_app.command("list")
def list_command(format: FormatOption = "human") -> None:
    """List browser sessions."""
    output_format = _parse_output_format(format)
    sessions = browser_session.list_sessions()
    if output_format == "json":
        _print_json({"sessions": sessions})
        return
    table = Table(title="WebSkrap Browser Sessions")
    table.add_column("Session")
    table.add_column("Running")
    table.add_column("PID")
    table.add_column("Port")
    table.add_column("Sandbox")
    for entry in sessions:
        sandbox = entry["chromium_sandbox"]
        table.add_row(
            str(entry["session"]),
            "yes" if entry["running"] else "no",
            str(entry["pid"] or ""),
            str(entry["port"] or ""),
            "" if sandbox is None else ("yes" if sandbox else "no"),
        )
    console.print(table)


@browser_app.command("goto")
def goto_command(
    url: Annotated[str, typer.Argument(help="URL to navigate to.")],
    session: SessionOption = "default",
    wait_until: Annotated[
        str,
        typer.Option("--wait-until", help="commit, domcontentloaded, load, or networkidle."),
    ] = "load",
    timeout_ms: ActionTimeoutOption = DEFAULT_NAVIGATION_TIMEOUT_MS,
    format: FormatOption = "human",
) -> None:
    """Navigate the current page."""
    output_format = _parse_output_format(format)
    parsed_wait_until = _parse_wait_until(wait_until)
    state = _run_page_command(
        session,
        lambda page: browser_session.goto(page, url, parsed_wait_until),
        timeout_ms=timeout_ms,
    )
    _emit_state(state, output_format)


def _navigation_command(name: str, help_text: str, method: str) -> None:
    @browser_app.command(name, help=help_text)
    def command(
        session: SessionOption = "default",
        timeout_ms: ActionTimeoutOption = DEFAULT_NAVIGATION_TIMEOUT_MS,
        format: FormatOption = "human",
    ) -> None:
        output_format = _parse_output_format(format)

        async def action(page: Page) -> dict[str, Any]:
            await getattr(page, method)()
            return await browser_session.page_state(page)

        _emit_state(_run_page_command(session, action, timeout_ms=timeout_ms), output_format)


_navigation_command("back", "Go back in the current page's history.", "go_back")
_navigation_command("forward", "Go forward in the current page's history.", "go_forward")
_navigation_command("reload", "Reload the current page.", "reload")


@browser_app.command("snapshot")
def snapshot_command(
    session: SessionOption = "default",
    depth: Annotated[
        int | None, typer.Option("--depth", min=1, help="Maximum snapshot tree depth.")
    ] = None,
    max_chars: Annotated[
        int, typer.Option("--max-chars", min=0, help="Maximum snapshot characters.")
    ] = 20_000,
    offset: Annotated[
        int,
        typer.Option(
            "--offset",
            min=0,
            help="Start at this character; pass back next_snapshot_offset to continue.",
        ),
    ] = 0,
    format: FormatOption = "human",
) -> None:
    """Print an aria snapshot of the page with `eN` element refs."""
    output_format = _parse_output_format(format)
    result = browser_session.shape_snapshot(
        _run_page_command(session, lambda page: browser_session.snapshot(page, depth=depth)),
        max_chars,
        offset,
    )
    if output_format == "json":
        _print_json(result)
        return
    _emit_state({"url": result["url"], "title": result["title"]}, output_format)
    typer.echo(result["snapshot"])
    if result["snapshot_truncated"]:
        stderr_console.print(
            f"[yellow]truncated:[/yellow] {result['snapshot_length']} characters; "
            f"continue with --offset {result['next_snapshot_offset']}"
        )


def _register_element_command(name: str, help_text: str) -> None:
    """Register an interaction command taking a snapshot ref or selector."""

    @browser_app.command(name, help=help_text)
    def command(
        target: Annotated[str, typer.Argument(help="Snapshot ref (e12) or Playwright selector.")],
        value: Annotated[list[str] | None, typer.Argument(help="Value(s) for the action.")] = None,
        session: SessionOption = "default",
        timeout_ms: ActionTimeoutOption = DEFAULT_ACTION_TIMEOUT_MS,
        format: FormatOption = "human",
    ) -> None:
        output_format = _parse_output_format(format)
        values = value or []
        try:
            # Fail on a bad value count before connecting to the browser.
            browser_session.element_arguments(name, values)
        except WebSkrapError as exc:
            _fail(str(exc))

        async def action(page: Page) -> dict[str, Any]:
            await browser_session.element_action(page, name, target, values)
            return await browser_session.page_state(page)

        _emit_state(_run_page_command(session, action, timeout_ms=timeout_ms), output_format)


for _name in ELEMENT_ACTIONS:
    _register_element_command(_name, _ELEMENT_COMMAND_HELP[_name])


@browser_app.command("press")
def press_command(
    key: Annotated[str, typer.Argument(help="Key to press, e.g. Enter or Control+a.")],
    session: SessionOption = "default",
    timeout_ms: ActionTimeoutOption = DEFAULT_ACTION_TIMEOUT_MS,
    format: FormatOption = "human",
) -> None:
    """Press a key on the page."""
    output_format = _parse_output_format(format)

    async def action(page: Page) -> dict[str, Any]:
        await page.keyboard.press(key)
        return await browser_session.page_state(page)

    _emit_state(_run_page_command(session, action, timeout_ms=timeout_ms), output_format)


@browser_app.command("wait")
def wait_command(
    session: SessionOption = "default",
    text: Annotated[
        str | None, typer.Option("--text", help="Wait until this text is visible.")
    ] = None,
    text_gone: Annotated[
        str | None, typer.Option("--text-gone", help="Wait until this text is gone.")
    ] = None,
    selector: Annotated[
        str | None,
        typer.Option("--selector", help="Wait on a snapshot ref (e12) or Playwright selector."),
    ] = None,
    state: Annotated[
        str,
        typer.Option("--state", help="attached, detached, visible, or hidden (with --selector)."),
    ] = "visible",
    load_state: Annotated[
        str | None,
        typer.Option("--load-state", help="domcontentloaded, load, or networkidle."),
    ] = None,
    timeout_ms: ActionTimeoutOption = DEFAULT_ACTION_TIMEOUT_MS,
    format: FormatOption = "human",
) -> None:
    """Wait for one condition on the page (text, selector, or load state)."""
    output_format = _parse_output_format(format)
    parsed_state = _parse_element_state(state)
    parsed_load_state = _parse_load_state(load_state) if load_state is not None else None
    try:
        # Fail before connecting when the conditions do not add up to exactly one.
        browser_session.wait_condition(text, text_gone, selector, parsed_load_state)
    except WebSkrapError as exc:
        _fail(str(exc))

    state_payload = _run_page_command(
        session,
        lambda page: browser_session.wait_for(
            page,
            text=text,
            text_gone=text_gone,
            selector=selector,
            selector_state=parsed_state,
            load_state=parsed_load_state,
            timeout_ms=timeout_ms,
        ),
        timeout_ms=timeout_ms,
    )
    if output_format == "human":
        console.print(f"[bold]Matched:[/bold] {state_payload['matched']}")
    _emit_state(state_payload, output_format)


@browser_app.command("screenshot")
def screenshot_command(
    path: Annotated[Path | None, typer.Argument(help="Output PNG path.")] = None,
    session: SessionOption = "default",
    full_page: Annotated[
        bool, typer.Option("--full-page", help="Capture the full scrollable page.")
    ] = False,
    format: FormatOption = "human",
) -> None:
    """Screenshot the current page."""
    output_format = _parse_output_format(format)
    target = path or Path(f"webskrap-{uuid4().hex}.png")

    async def action(page: Page) -> dict[str, Any]:
        target.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(target), full_page=full_page)
        return {**await browser_session.page_state(page), "path": str(target)}

    result = _run_page_command(session, action)
    if output_format == "json":
        _print_json(result)
        return
    console.print(f"[bold]Screenshot:[/bold] {result['path']}")


@browser_app.command("eval")
def eval_command(
    expression: Annotated[str, typer.Argument(help="JavaScript expression or function.")],
    session: SessionOption = "default",
    timeout_ms: ActionTimeoutOption = DEFAULT_ACTION_TIMEOUT_MS,
    max_chars: Annotated[
        int, typer.Option("--max-chars", min=0, help="Maximum encoded result characters.")
    ] = 20_000,
    format: FormatOption = "human",
) -> None:
    """Evaluate JavaScript on the current page and print the JSON result."""
    output_format = _parse_output_format(format)

    async def action(page: Page) -> Any:
        return await page.evaluate(expression)

    payload = browser_session.shape_eval_result(
        _run_page_command(session, action, timeout_ms=timeout_ms), max_chars
    )
    if output_format == "json":
        _print_json(payload)
        return
    if payload["result_truncated"]:
        typer.echo(payload["result_json"])
        stderr_console.print(
            f"[yellow]truncated:[/yellow] {payload['result_length']} encoded characters"
        )
        return
    typer.echo(json.dumps(payload["result"], ensure_ascii=False))
