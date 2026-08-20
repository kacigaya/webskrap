"""Persistent interactive browser commands (`webskrap browser ...`).

A native Python take on the official Playwright CLI (`@playwright/cli`):
`open` launches a detached Chromium that outlives each CLI invocation, and
every other command reconnects to it over CDP, acts on the current page, and
exits. Snapshots use Playwright's AI aria snapshot, so elements carry `eN`
refs that interaction commands accept directly (`webskrap browser click e12`).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn, TypeVar
from uuid import uuid4

import typer
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright
from rich.console import Console
from rich.table import Table

from webskrap.parsing import parse_wait_until

browser_app = typer.Typer(
    help="Drive a persistent browser with Playwright CLI-style commands.",
    no_args_is_help=True,
)
console = Console()
stderr_console = Console(stderr=True, highlight=False)

T = TypeVar("T")
OutputFormat = Literal["human", "json"]

SESSION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
SNAPSHOT_REF_PATTERN = re.compile(r"^e\d+$")
DEVTOOLS_PORT_FILE = "DevToolsActivePort"
LAUNCH_TIMEOUT_S = 20.0
CLOSE_TIMEOUT_S = 5.0
DEFAULT_ACTION_TIMEOUT_MS = 10_000.0
DEFAULT_NAVIGATION_TIMEOUT_MS = 30_000.0

SessionOption = Annotated[str, typer.Option("--session", "-s", help="Browser session name.")]
FormatOption = Annotated[str, typer.Option("--format", help="Output format: human or json.")]
ActionTimeoutOption = Annotated[float, typer.Option("--timeout-ms", min=1, help="Action timeout.")]


def _sessions_root() -> Path:
    if override := os.environ.get("WEBSKRAP_BROWSER_DIR"):
        return Path(override)
    return Path.home() / ".webskrap" / "browser"


def _session_dir(name: str) -> Path:
    # Session names become directory names, so reject path separators and
    # anything else that could escape the sessions root.
    if not SESSION_NAME_PATTERN.fullmatch(name):
        _fail(f"invalid session name '{name}': use letters, digits, '.', '_' or '-'")
    return _sessions_root() / name


def _state_path(session_dir: Path) -> Path:
    return session_dir / "state.json"


def _read_state(session_dir: Path) -> dict[str, Any] | None:
    try:
        state = json.loads(_state_path(session_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or "pid" not in state or "port" not in state:
        return None
    return state


def _write_state(session_dir: Path, state: dict[str, Any]) -> None:
    _state_path(session_dir).write_text(json.dumps(state), encoding="utf-8")


def _session_running(session_dir: Path, state: dict[str, Any] | None) -> bool:
    """True when the recorded PID is alive and is this session's browser.

    PIDs get recycled, so before trusting (or killing) one, require its
    command line to reference this session's user-data directory.
    """
    if state is None:
        return False
    pid = state["pid"]
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        # No /proc (non-Linux): fall back to a liveness-only check.
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    return str(session_dir / "user-data").encode() in cmdline


def _fail(message: str) -> NoReturn:
    stderr_console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


def _parse_output_format(value: str) -> OutputFormat:
    if value not in ("human", "json"):
        raise typer.BadParameter("must be one of: human, json")
    return value


def _print_json(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False))


async def _chromium_executable() -> str:
    async with async_playwright() as playwright:
        path = playwright.chromium.executable_path
    if Path(path).exists():
        return path
    try:
        from patchright.async_api import async_playwright as patchright_playwright
    except ImportError:  # pragma: no cover - patchright ships with webskrap
        _fail("Chromium is not installed. Run: webskrap install")
    async with patchright_playwright() as playwright:
        path = playwright.chromium.executable_path
    if Path(path).exists():
        return path
    _fail("Chromium is not installed. Run: webskrap install")


def _launch_browser(session_dir: Path, *, executable: str, headless: bool) -> tuple[int, int]:
    """Start a detached Chromium and return its (pid, CDP port)."""
    user_data_dir = session_dir / "user-data"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    port_file = user_data_dir / DEVTOOLS_PORT_FILE
    port_file.unlink(missing_ok=True)
    log_path = session_dir / "browser.log"

    command = [
        executable,
        "--remote-debugging-port=0",
        f"--user-data-dir={user_data_dir}",
        # Playwright launches Chromium with the sandbox disabled by default
        # (chromium_sandbox=False); mirror that so `open` works wherever
        # `webskrap fetch` does.
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        # Playwright also disables the back/forward cache: a bfcache restore
        # re-fires no load events, which strands go_back/go_forward waits.
        "--disable-features=BackForwardCache",
    ]
    if headless:
        command.append("--headless=new")
    command.append("about:blank")

    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    return process.pid, _wait_for_devtools_port(port_file, process, log_path)


def _wait_for_devtools_port(
    port_file: Path, process: subprocess.Popen[bytes], log_path: Path
) -> int:
    deadline = time.monotonic() + LAUNCH_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _fail(f"browser exited during startup; see {log_path}")
        try:
            first_line = port_file.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            first_line = ""
        if first_line.isdigit():
            return int(first_line)
        time.sleep(0.05)
    process.terminate()
    _fail(f"browser did not report a DevTools port within {LAUNCH_TIMEOUT_S:.0f}s")


def _run_page_command(
    session: str,
    action: Callable[[Page], Awaitable[T]],
    *,
    timeout_ms: float = DEFAULT_ACTION_TIMEOUT_MS,
) -> T:
    try:
        return asyncio.run(_run_page_action(session, action, timeout_ms))
    except PlaywrightError as exc:
        _fail(str(exc).strip().splitlines()[0])


async def _run_page_action(
    session: str,
    action: Callable[[Page], Awaitable[T]],
    timeout_ms: float,
) -> T:
    session_dir = _session_dir(session)
    state = _read_state(session_dir)
    if state is None:
        _fail(f"session '{session}' is not open. Run: webskrap browser open")
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{state['port']}"
            )
        except PlaywrightError:
            _fail(
                f"session '{session}' is not reachable. "
                "Run: webskrap browser close, then webskrap browser open"
            )
        try:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[-1] if context.pages else await context.new_page()
            page.set_default_timeout(timeout_ms)
            page.set_default_navigation_timeout(timeout_ms)
            return await action(page)
        finally:
            await browser.close()


def _target_selector(target: str) -> str:
    """Map a snapshot ref like `e12` to its selector; pass selectors through."""
    if SNAPSHOT_REF_PATTERN.fullmatch(target):
        return f"aria-ref={target}"
    return target


async def _resolve_locator(page: Page, target: str):
    """Return a locator for a snapshot ref (`e12`) or a Playwright selector.

    Refs only resolve against an aria snapshot taken on the current CDP
    connection, and each command runs on a fresh one, so retake the snapshot
    first. Refs are stable for an unchanged DOM; after a DOM change they are
    stale and the caller should run `snapshot` again.
    """
    if SNAPSHOT_REF_PATTERN.fullmatch(target):
        await page.locator("body").aria_snapshot(mode="ai")
    return page.locator(_target_selector(target))


async def _page_state(page: Page) -> dict[str, Any]:
    return {"url": page.url, "title": await page.title()}


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
    format: FormatOption = "human",
) -> None:
    """Start (or reuse) a persistent browser session."""
    output_format = _parse_output_format(format)
    session_dir = _session_dir(session)
    existing = _read_state(session_dir)
    state = existing if _session_running(session_dir, existing) else None
    reused = state is not None

    if state is None:
        executable = asyncio.run(_chromium_executable())
        pid, port = _launch_browser(session_dir, executable=executable, headless=not headed)
        state = {
            "pid": pid,
            "port": port,
            "headless": not headed,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        _write_state(session_dir, state)

    payload: dict[str, Any] = {
        "session": session,
        "pid": state["pid"],
        "port": state["port"],
        "reused": reused,
    }
    if url:
        payload.update(
            _run_page_command(
                session,
                lambda page: _goto(page, url, "load"),
                timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS,
            )
        )

    if output_format == "json":
        _print_json(payload)
        return
    verb = "Reusing" if reused else "Opened"
    console.print(f"{verb} session [bold]{session}[/bold] (pid {state['pid']})")
    if url:
        _emit_state(payload, output_format)


async def _goto(page: Page, url: str, wait_until: str) -> dict[str, Any]:
    response = await page.goto(url, wait_until=parse_wait_until(wait_until))
    return {"status": response.status if response else None, **await _page_state(page)}


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
    if all_sessions:
        root = _sessions_root()
        names = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    else:
        if _read_state(_session_dir(session)) is None and not delete_data:
            _fail(f"session '{session}' is not open")
        names = [session]

    closed = [_close_session(name, delete_data=delete_data) for name in names]
    if output_format == "json":
        _print_json({"closed": closed})
        return
    for entry in closed:
        console.print(f"closed [bold]{entry['session']}[/bold]")


def _close_session(name: str, *, delete_data: bool) -> dict[str, Any]:
    session_dir = _session_dir(name)
    state = _read_state(session_dir)
    if state is not None and _session_running(session_dir, state):
        _terminate(session_dir, state["pid"])
    _state_path(session_dir).unlink(missing_ok=True)
    if delete_data:
        shutil.rmtree(session_dir, ignore_errors=True)
    return {"session": name, "deleted_data": delete_data}


def _terminate(session_dir: Path, pid: int) -> None:
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + CLOSE_TIMEOUT_S
    while time.monotonic() < deadline:
        if not _session_running(session_dir, {"pid": pid, "port": 0}):
            return
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)


@browser_app.command("list")
def list_command(format: FormatOption = "human") -> None:
    """List browser sessions."""
    output_format = _parse_output_format(format)
    root = _sessions_root()
    sessions = []
    if root.is_dir():
        for path in sorted(p for p in root.iterdir() if p.is_dir()):
            state = _read_state(path)
            running = _session_running(path, state)
            sessions.append(
                {
                    "session": path.name,
                    "running": running,
                    "pid": state["pid"] if running and state else None,
                    "port": state["port"] if running and state else None,
                }
            )
    if output_format == "json":
        _print_json({"sessions": sessions})
        return
    table = Table(title="WebSkrap Browser Sessions")
    table.add_column("Session")
    table.add_column("Running")
    table.add_column("PID")
    table.add_column("Port")
    for entry in sessions:
        table.add_row(
            str(entry["session"]),
            "yes" if entry["running"] else "no",
            str(entry["pid"] or ""),
            str(entry["port"] or ""),
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
    state = _run_page_command(
        session, lambda page: _goto(page, url, wait_until), timeout_ms=timeout_ms
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
            return await _page_state(page)

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
    format: FormatOption = "human",
) -> None:
    """Print an aria snapshot of the page with `eN` element refs."""
    output_format = _parse_output_format(format)

    async def action(page: Page) -> dict[str, Any]:
        snapshot = await page.locator("body").aria_snapshot(mode="ai", depth=depth)
        return {**await _page_state(page), "snapshot": snapshot}

    result = _run_page_command(session, action)
    if output_format == "json":
        _print_json(result)
        return
    _emit_state({"url": result["url"], "title": result["title"]}, output_format)
    typer.echo(result["snapshot"])


# (command, Locator method, value arity, help)
_ELEMENT_COMMANDS: tuple[tuple[str, str, str, str], ...] = (
    ("click", "click", "none", "Click an element."),
    ("dblclick", "dblclick", "none", "Double-click an element."),
    ("hover", "hover", "none", "Hover over an element."),
    ("check", "check", "none", "Check a checkbox or radio."),
    ("uncheck", "uncheck", "none", "Uncheck a checkbox."),
    ("fill", "fill", "one", "Fill an input with a value."),
    ("type", "press_sequentially", "one", "Type text into an element key by key."),
    ("select", "select_option", "many", "Select option value(s) in a <select>."),
)


def _element_arguments(name: str, arity: str, values: list[str]) -> list[Any]:
    if arity == "none":
        if values:
            _fail(f"'{name}' takes no value argument")
        return []
    if arity == "one":
        if len(values) != 1:
            _fail(f"'{name}' takes exactly one value argument")
        return [values[0]]
    if not values:
        _fail(f"'{name}' takes at least one value argument")
    return [values]


def _register_element_command(name: str, method: str, arity: str, help_text: str) -> None:
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
        arguments = _element_arguments(name, arity, value or [])

        async def action(page: Page) -> dict[str, Any]:
            locator = await _resolve_locator(page, target)
            await getattr(locator, method)(*arguments)
            return await _page_state(page)

        _emit_state(_run_page_command(session, action, timeout_ms=timeout_ms), output_format)


for _name, _method, _arity, _help in _ELEMENT_COMMANDS:
    _register_element_command(_name, _method, _arity, _help)


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
        return await _page_state(page)

    _emit_state(_run_page_command(session, action, timeout_ms=timeout_ms), output_format)


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
        return {**await _page_state(page), "path": str(target)}

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
    format: FormatOption = "human",
) -> None:
    """Evaluate JavaScript on the current page and print the JSON result."""
    output_format = _parse_output_format(format)

    async def action(page: Page) -> Any:
        return await page.evaluate(expression)

    result = _run_page_command(session, action, timeout_ms=timeout_ms)
    if output_format == "json":
        _print_json({"result": result})
        return
    typer.echo(json.dumps(result, ensure_ascii=False))
