"""Persistent interactive browser sessions shared by the CLI and MCP server.

``open_session`` launches a detached Chromium that outlives the calling
process; every action reconnects to it over CDP, acts on the current page, and
disconnects. Snapshots use Playwright's AI aria snapshot, so elements carry
``eN`` refs that interaction helpers accept alongside Playwright selectors.

All failures raise :class:`~webskrap.client.WebSkrapError` (or propagate
Playwright errors); presentation layers translate them for their medium.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess  # nosec B404  # noqa: S404 - fixed argv for Chromium, never a shell
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page, async_playwright

from webskrap.client import WebSkrapError
from webskrap.models import WaitUntil
from webskrap.paths import secure_directory

T = TypeVar("T")

SESSION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
SNAPSHOT_REF_PATTERN = re.compile(r"^e\d+$")
DEVTOOLS_PORT_FILE = "DevToolsActivePort"
LAUNCH_TIMEOUT_S = 20.0
CLOSE_TIMEOUT_S = 5.0
DEFAULT_ACTION_TIMEOUT_MS = 10_000.0
DEFAULT_NAVIGATION_TIMEOUT_MS = 30_000.0
SANDBOX_ENV = "WEBSKRAP_CHROMIUM_SANDBOX"
# Chromium prints one of these when its setuid/namespace sandbox cannot start
# (unprivileged user namespaces off, or running as root in a container).
SANDBOX_FAILURE_MARKERS = (
    "no usable sandbox",
    "sandboxhelper",
    "setuid sandbox",
    "clone_newuser",
    "--no-sandbox",
)

# name -> (Locator method, value arity: "none" | "one" | "many")
ELEMENT_ACTIONS: dict[str, tuple[str, str]] = {
    "click": ("click", "none"),
    "dblclick": ("dblclick", "none"),
    "hover": ("hover", "none"),
    "check": ("check", "none"),
    "uncheck": ("uncheck", "none"),
    "fill": ("fill", "one"),
    "type": ("press_sequentially", "one"),
    "select": ("select_option", "many"),
}


def sessions_root() -> Path:
    """Return the directory holding every persistent session's profile data.

    ``WEBSKRAP_BROWSER_DIR`` overrides the default ``~/.webskrap/browser``.
    """
    if override := os.environ.get("WEBSKRAP_BROWSER_DIR"):
        return Path(override)
    return Path.home() / ".webskrap" / "browser"


def sandbox_enabled(chromium_sandbox: bool | None = None) -> bool:
    """Resolve whether a session's Chromium keeps its OS sandbox.

    An explicit ``chromium_sandbox`` argument wins. Otherwise the
    ``WEBSKRAP_CHROMIUM_SANDBOX`` environment variable decides, so deployments
    that cannot sandbox (unprivileged containers) can opt out for callers such
    as the MCP server, which deliberately does not expose the switch to models.
    Sandboxing is on unless something explicitly turns it off.
    """
    if chromium_sandbox is not None:
        return chromium_sandbox
    value = os.environ.get(SANDBOX_ENV)
    if value is None:
        return True
    return value.strip().lower() not in ("0", "false", "no", "off")


def session_dir(name: str) -> Path:
    """Return a session's directory, rejecting names that escape the root.

    Raises:
        WebSkrapError: If ``name`` is ``.`` or ``..``, or contains characters
            other than letters, digits, ``.``, ``_`` or ``-``, or if its
            directory is a symlink.
    """
    # Session names become directory names, so reject path separators and
    # anything else that could escape the sessions root.
    if name in {".", ".."} or not SESSION_NAME_PATTERN.fullmatch(name):
        msg = (
            f"invalid session name '{name}': use letters, digits, '.', '_' or '-', "
            "but not '.' or '..'"
        )
        raise WebSkrapError(msg)
    directory = sessions_root() / name
    if directory.is_symlink():
        msg = f"session directory must not be a symlink: {directory}"
        raise WebSkrapError(msg)
    return directory


def create_session_dir(name: str) -> Path:
    """Create a session's directory tree owner-only and return it.

    Profile data under it holds cookies and logged-in state, so the session
    directory is created (or tightened to) ``0700``. The sessions root is only
    tightened when WebSkrap creates it: a ``WEBSKRAP_BROWSER_DIR`` the user
    already set up may be shared on purpose, and the per-session directories
    inside it are what actually hold the cookies.
    """
    directory = session_dir(name)
    secure_directory(sessions_root(), tighten_existing=False)
    return secure_directory(directory)


def state_path(directory: Path) -> Path:
    """Return the path of a session directory's state file."""
    return directory / "state.json"


def read_state(directory: Path) -> dict[str, Any] | None:
    """Return a session's recorded state, or None when missing or unreadable."""
    try:
        state = json.loads(state_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or "pid" not in state or "port" not in state:
        return None
    return state


def write_state(directory: Path, state: dict[str, Any]) -> None:
    """Write a session's state file so readers never see a partial file."""
    # Atomic replace: a concurrent action must never read a half-written file.
    temp_path = state_path(directory).with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temp_path, state_path(directory))


def session_running(directory: Path, state: dict[str, Any] | None) -> bool:
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
    return str(directory / "user-data").encode() in cmdline


def is_session_dir(path: Path) -> bool:
    """True when ``path`` looks like a session directory this tool manages."""
    return (
        path.is_dir()
        and SESSION_NAME_PATTERN.fullmatch(path.name) is not None
        and (state_path(path).exists() or (path / "user-data").is_dir())
    )


async def chromium_executable() -> str:
    """Return the Chromium binary path, preferring Playwright's download.

    Raises:
        WebSkrapError: If neither Playwright nor Patchright has Chromium
            installed.
    """
    async with async_playwright() as playwright:
        path = playwright.chromium.executable_path
    if Path(path).exists():
        return path
    try:
        from patchright.async_api import async_playwright as patchright_playwright
    except ImportError as exc:  # pragma: no cover - patchright ships with webskrap
        msg = "Chromium is not installed. Run: webskrap install"
        raise WebSkrapError(msg) from exc
    async with patchright_playwright() as playwright:
        path = playwright.chromium.executable_path
    if Path(path).exists():
        return path
    msg = "Chromium is not installed. Run: webskrap install"
    raise WebSkrapError(msg)


def signal_group(pid: int, sig: signal.Signals) -> None:
    """Signal the browser's whole process group; a vanished group is fine.

    The browser is launched with ``start_new_session=True``, so its PID is the
    group leader. Signalling only the leader (e.g. SIGKILL) would orphan
    renderer children that keep holding the profile's SingletonLock.
    """
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        with suppress(ProcessLookupError):
            os.kill(pid, sig)


def launch_browser(
    directory: Path,
    *,
    executable: str,
    headless: bool,
    chromium_sandbox: bool = True,
) -> tuple[int, int]:
    """Start a detached Chromium and return its (pid, CDP port).

    Args:
        directory: Session directory; the profile lives in ``user-data`` below it.
        executable: Chromium binary to run.
        headless: Launch without a visible window.
        chromium_sandbox: Keep Chromium's OS sandbox. Passing False adds
            ``--no-sandbox``, which lets a compromised renderer processing a
            hostile page reach the rest of the machine. Only do that where the
            sandbox genuinely cannot start.

    Raises:
        WebSkrapError: If the browser exits during startup or never reports a
            DevTools port.
    """
    user_data_dir = secure_directory(directory / "user-data")
    port_file = user_data_dir / DEVTOOLS_PORT_FILE
    port_file.unlink(missing_ok=True)
    log_path = directory / "browser.log"

    command = [
        executable,
        "--remote-debugging-port=0",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # A bfcache restore re-fires no load events, stranding the
        # go_back/go_forward load wait. Playwright's own launcher keeps bfcache
        # and tracks restores internally, but that tracking is unavailable when
        # attaching over CDP, so trade bfcache for deterministic load events.
        "--disable-features=BackForwardCache",
    ]
    if not chromium_sandbox:
        command.append("--no-sandbox")
    if headless:
        command.append("--headless=new")
    command.append("about:blank")

    with log_path.open("wb") as log:
        # Fixed argv, no shell. The executable comes from the installed
        # Playwright/Patchright browser, and every argument is either a
        # constant flag or this session's own validated directory -- nothing
        # here is reachable from page content.
        process = subprocess.Popen(  # nosec B603  # noqa: S603
            command,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    try:
        return process.pid, _wait_for_devtools_port(port_file, process, log_path)
    except BaseException:
        # Ctrl-C or a startup failure must not orphan a detached browser that
        # holds the profile lock with no state file pointing at it.
        signal_group(process.pid, signal.SIGKILL)
        raise


def _sandbox_hint(log_path: Path) -> str:
    """Return advice about the sandbox when the browser log blames it.

    Never retries without the sandbox: dropping it silently would turn a
    configuration problem into a permanent, invisible loss of isolation.
    """
    try:
        log = log_path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return ""
    if not any(marker in log for marker in SANDBOX_FAILURE_MARKERS):
        return ""
    return (
        ". Chromium's sandbox could not start. Enable unprivileged user "
        "namespaces, or accept the weaker isolation with "
        f"'webskrap browser open --no-sandbox' (or {SANDBOX_ENV}=0)"
    )


def _wait_for_devtools_port(
    port_file: Path, process: subprocess.Popen[bytes], log_path: Path
) -> int:
    deadline = time.monotonic() + LAUNCH_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            msg = f"browser exited during startup; see {log_path}{_sandbox_hint(log_path)}"
            raise WebSkrapError(msg)
        try:
            first_line = port_file.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            first_line = ""
        if first_line.isdigit():
            return int(first_line)
        time.sleep(0.05)
    msg = f"browser did not report a DevTools port within {LAUNCH_TIMEOUT_S:.0f}s"
    raise WebSkrapError(msg)


async def open_session(
    name: str,
    *,
    headless: bool = True,
    chromium_sandbox: bool | None = None,
) -> dict[str, Any]:
    """Start (or reuse) a persistent browser session.

    The session directory is created owner-only; it accumulates cookies and
    logged-in state that outlive the calling process.

    Args:
        name: Session name; letters, digits, ``.``, ``_`` or ``-``.
        headless: Launch without a visible window.
        chromium_sandbox: Keep Chromium's OS sandbox. None consults
            ``WEBSKRAP_CHROMIUM_SANDBOX`` and otherwise sandboxes. See
            :func:`sandbox_enabled` and :func:`launch_browser`.

    Returns:
        ``{"session", "pid", "port", "reused", "chromium_sandbox"}``.

    Raises:
        WebSkrapError: If the name is invalid or the browser fails to start.
    """
    # Unconditional, so a session directory created by an older version (or
    # under a loose umask) is tightened on the next open, not only on relaunch.
    directory = create_session_dir(name)
    existing = read_state(directory)
    state = existing if session_running(directory, existing) else None
    reused = state is not None

    if state is None:
        executable = await chromium_executable()
        sandboxed = sandbox_enabled(chromium_sandbox)
        pid, port = launch_browser(
            directory,
            executable=executable,
            headless=headless,
            chromium_sandbox=sandboxed,
        )
        state = {
            "pid": pid,
            "port": port,
            "headless": headless,
            # Recorded so `browser list` can show which running sessions gave
            # up renderer isolation; a reused session keeps its launch value.
            "chromium_sandbox": sandboxed,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        try:
            write_state(directory, state)
        except BaseException:
            # Without a state file the running browser would be unreachable
            # and would hold the profile lock against the next open.
            signal_group(pid, signal.SIGKILL)
            raise

    return {
        "session": name,
        "pid": state["pid"],
        "port": state["port"],
        "reused": reused,
        "chromium_sandbox": bool(state.get("chromium_sandbox", False)),
    }


def close_session(name: str, *, delete_data: bool = False) -> dict[str, Any]:
    """Stop a session's browser; profile data persists unless ``delete_data``."""
    directory = session_dir(name)
    state = read_state(directory)
    if state is not None and session_running(directory, state):
        _terminate(directory, state["pid"])
    state_path(directory).unlink(missing_ok=True)
    if delete_data:
        shutil.rmtree(directory, ignore_errors=True)
    return {"session": name, "deleted_data": delete_data}


def _terminate(directory: Path, pid: int) -> None:
    signal_group(pid, signal.SIGTERM)
    deadline = time.monotonic() + CLOSE_TIMEOUT_S
    while time.monotonic() < deadline:
        if not session_running(directory, {"pid": pid, "port": 0}):
            return
        time.sleep(0.05)
    signal_group(pid, signal.SIGKILL)


def list_session_names() -> list[str]:
    """Return the names of every on-disk session, sorted."""
    root = sessions_root()
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if is_session_dir(path))


def list_sessions() -> list[dict[str, Any]]:
    """Return one entry per session.

    Each is ``{"session", "running", "pid", "port", "chromium_sandbox"}``. The
    last three are None for a session that is not running; ``chromium_sandbox``
    reports how the running browser was launched, so an operator can see which
    sessions gave up renderer isolation.
    """
    sessions = []
    for name in list_session_names():
        directory = sessions_root() / name
        state = read_state(directory)
        running = session_running(directory, state)
        sessions.append(
            {
                "session": name,
                "running": running,
                "pid": state["pid"] if running and state else None,
                "port": state["port"] if running and state else None,
                "chromium_sandbox": (
                    bool(state.get("chromium_sandbox", False)) if running and state else None
                ),
            }
        )
    return sessions


async def run_page_action(
    name: str,
    action: Callable[[Page], Awaitable[T]],
    *,
    timeout_ms: float = DEFAULT_ACTION_TIMEOUT_MS,
) -> T:
    """Connect to the session's browser, run ``action`` on the current page."""
    directory = session_dir(name)
    state = read_state(directory)
    if state is None:
        msg = f"session '{name}' is not open. Run: webskrap browser open"
        raise WebSkrapError(msg)
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{state['port']}"
            )
        except PlaywrightError as exc:
            msg = (
                f"session '{name}' is not reachable. "
                "Run: webskrap browser close, then webskrap browser open"
            )
            raise WebSkrapError(msg) from exc
        try:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[-1] if context.pages else await context.new_page()
            page.set_default_timeout(timeout_ms)
            page.set_default_navigation_timeout(timeout_ms)
            return await action(page)
        finally:
            await browser.close()


def target_selector(target: str) -> str:
    """Map a snapshot ref like ``e12`` to its selector; pass selectors through."""
    if SNAPSHOT_REF_PATTERN.fullmatch(target):
        return f"aria-ref={target}"
    return target


async def resolve_locator(page: Page, target: str) -> Locator:
    """Return a locator for a snapshot ref (``e12``) or a Playwright selector.

    Refs only resolve against an aria snapshot taken on the current CDP
    connection, and each action runs on a fresh one, so retake the snapshot
    first. Refs are stable for an unchanged DOM; after a DOM change they are
    stale and the caller should snapshot again.
    """
    if SNAPSHOT_REF_PATTERN.fullmatch(target):
        await page.locator("body").aria_snapshot(mode="ai")
    return page.locator(target_selector(target))


def element_arguments(action: str, values: list[str]) -> list[Any]:
    """Validate ``values`` against an :data:`ELEMENT_ACTIONS` entry's arity."""
    if action not in ELEMENT_ACTIONS:
        supported = ", ".join(ELEMENT_ACTIONS)
        msg = f"unknown action '{action}': use one of {supported}"
        raise WebSkrapError(msg)
    arity = ELEMENT_ACTIONS[action][1]
    if arity == "none":
        if values:
            msg = f"'{action}' takes no value argument"
            raise WebSkrapError(msg)
        return []
    if arity == "one":
        if len(values) != 1:
            msg = f"'{action}' takes exactly one value argument"
            raise WebSkrapError(msg)
        return [values[0]]
    if not values:
        msg = f"'{action}' takes at least one value argument"
        raise WebSkrapError(msg)
    return [values]


async def element_action(page: Page, action: str, target: str, values: list[str]) -> None:
    """Run an :data:`ELEMENT_ACTIONS` interaction against ``target``."""
    arguments = element_arguments(action, values)
    locator = await resolve_locator(page, target)
    await getattr(locator, ELEMENT_ACTIONS[action][0])(*arguments)


async def page_state(page: Page) -> dict[str, Any]:
    """Return the page's current ``{"url", "title"}``."""
    return {"url": page.url, "title": await page.title()}


async def goto(page: Page, url: str, wait_until: WaitUntil) -> dict[str, Any]:
    """Navigate ``page`` to ``url`` and return its status and state."""
    response = await page.goto(url, wait_until=wait_until)
    return {"status": response.status if response else None, **await page_state(page)}


async def snapshot(page: Page, *, depth: int | None = None) -> dict[str, Any]:
    """Return the page state plus an aria snapshot carrying ``eN`` refs."""
    tree = await page.locator("body").aria_snapshot(mode="ai", depth=depth)
    return {**await page_state(page), "snapshot": tree}
