"""Minimal Chrome DevTools Protocol client over Chromium's debugging pipe.

This is WebSkrap's own driver core: no Playwright, no Node.js process, and no
third-party WebSocket library. Chromium is spawned with
``--remote-debugging-pipe``, which reads NUL-terminated JSON commands on file
descriptor 3 and writes replies and events on descriptor 4. No TCP port is
opened, so no other local process can attach to the browser.

The client sends only the commands a caller asks for. It never enables the
``Runtime`` or ``Console`` domains, whose events detectors can observe from the
page. Page scripts run in an isolated world (see :meth:`CDPPage.evaluate`), so
page code cannot see or patch the objects they use.

Deliberate limits of this prototype: POSIX only (``os.posix_spawn`` maps the
pipe descriptors), Chromium only, and one page per fetch with no auto-attach to
out-of-process iframes or workers.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any
from uuid import uuid4

from webskrap.errors import SANDBOX_FAILURE_MARKERS, ErrorCode, WebSkrapError

logger = logging.getLogger(__name__)

#: Callback for one CDP event; receives the event's ``params``.
EventHandler = Callable[[dict[str, Any]], None]

# Pipe messages can be large: a full-page screenshot arrives as one base64
# string. The reader buffers up to this many bytes for a single message.
MAX_MESSAGE_BYTES = 256 * 1024 * 1024
CLOSE_TIMEOUT_S = 5.0
_LOG_TAIL_BYTES = 4096
_POSIX_ONLY = "the CDP driver needs Linux or macOS (posix_spawn with pipe descriptors)"
# Runs a caller's script the way Playwright's evaluate does, in one round trip.
# The parse check (new Function only compiles) decides between a single
# expression, whose function value is called with the argument, and statements
# such as "const a = 1; a", which run as written. Nothing runs twice, and
# Runtime.compileScript is avoided because it needs Runtime.enable. Page CSP
# does not block eval in a DevTools isolated world. The newline keeps a
# trailing line comment from swallowing the closing paren.
_EVALUATE_SCRIPT = """async (source, arg) => {
  let single = true;
  try { new Function('return (' + source + '\\n)'); }
  catch (error) { if (!(error instanceof SyntaxError)) throw error; single = false; }
  const value = single ? (0, eval)('(' + source + '\\n)') : (0, eval)(source);
  return typeof value === 'function' ? await value(arg) : await value;
}"""


class CDPError(WebSkrapError):
    """A CDP command returned an error object instead of a result."""


class CDPConnection:
    """Routes commands, replies and events over one pipe pair.

    Commands get increasing ids and resolve when the matching reply arrives.
    Events go to handlers registered per ``(session_id, method)``; the browser
    target uses session id None. When the pipe closes, every pending command
    fails with the error given to :meth:`close`.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.WriteTransport,
        read_transport: asyncio.BaseTransport | None = None,
    ) -> None:
        """Wrap an open reader and writer; call :meth:`start` to begin reading."""
        self._reader = reader
        self._writer = writer
        self._read_transport = read_transport
        self._ids = count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._handlers: dict[tuple[str | None, str], list[EventHandler]] = {}
        self._read_task: asyncio.Task[None] | None = None
        self._closed_error: WebSkrapError | None = None
        self.on_disconnect: Callable[[], WebSkrapError] | None = None
        #: Reply timeout for commands sent without ``timeout_s``.
        self.default_timeout_s: float = 30.0

    def start(self) -> None:
        """Start the background reader. Idempotent."""
        if self._read_task is None:
            self._read_task = asyncio.create_task(self._read_loop())

    async def send(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Send one command and return its ``result`` object.

        Raises:
            CDPError: If the browser answered with an error.
            WebSkrapError: If the pipe is closed (``browser_launch``) or the
                reply does not arrive within ``timeout_s`` (``timeout``).
        """
        if self._closed_error is not None:
            raise self._closed_error
        message_id = next(self._ids)
        message: dict[str, Any] = {"id": message_id, "method": method, "params": dict(params or {})}
        if session_id is not None:
            message["sessionId"] = session_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            self._writer.write(json.dumps(message).encode() + b"\0")
            timeout_s = self.default_timeout_s if timeout_s is None else timeout_s
            return await asyncio.wait_for(future, timeout_s)
        except TimeoutError as exc:
            msg = f"CDP command {method} timed out after {timeout_s:.0f}s"
            raise WebSkrapError(msg, code=ErrorCode.TIMEOUT) from exc
        finally:
            self._pending.pop(message_id, None)

    def on(self, method: str, handler: EventHandler, *, session_id: str | None = None) -> None:
        """Call ``handler`` for every ``method`` event from ``session_id``."""
        self._handlers.setdefault((session_id, method), []).append(handler)

    def off(self, method: str, handler: EventHandler, *, session_id: str | None = None) -> None:
        """Remove a handler added with :meth:`on`; absent handlers are ignored."""
        handlers = self._handlers.get((session_id, method), [])
        with contextlib.suppress(ValueError):
            handlers.remove(handler)

    async def close(self, error: WebSkrapError | None = None) -> None:
        """Stop reading, close the write end, and fail pending commands."""
        self._fail_pending(error or WebSkrapError("CDP connection closed"))
        self._writer.close()
        if self._read_transport is not None:
            self._read_transport.close()
        if self._read_task is not None and self._read_task is not asyncio.current_task():
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task

    def _fail_pending(self, error: WebSkrapError) -> None:
        if self._closed_error is None:
            self._closed_error = error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(self._closed_error)
        self._pending.clear()

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self._reader.readuntil(b"\0")
                self._dispatch(json.loads(raw[:-1]))
        except (asyncio.IncompleteReadError, ConnectionError):
            error = (
                self.on_disconnect()
                if self.on_disconnect
                else WebSkrapError("browser closed the CDP pipe", code=ErrorCode.BROWSER_LAUNCH)
            )
            self._fail_pending(error)

    def _dispatch(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if message_id is not None:
            future = self._pending.get(message_id)
            if future is None or future.done():
                return
            if "error" in message:
                error = message["error"]
                future.set_exception(
                    CDPError(f"CDP error {error.get('code')}: {error.get('message')}")
                )
            else:
                future.set_result(message.get("result", {}))
            return
        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params", {})
        for handler in list(self._handlers.get((message.get("sessionId"), method), [])):
            try:
                handler(params)
            except Exception:
                # One broken handler must not stop the reader, or every
                # pending command would wait for its timeout.
                logger.exception("CDP event handler for %s failed", method)


class EventRecorder:
    """Collect events from the moment it starts, so none is missed by a late wait.

    A navigation can fire ``DOMContentLoaded`` before the command that started
    it returns. Recording first and waiting second avoids that race.
    """

    def __init__(
        self, connection: CDPConnection, methods: Iterable[str], *, session_id: str | None
    ) -> None:
        """Start recording ``methods`` events from ``session_id``."""
        self._connection = connection
        self._session_id = session_id
        self._methods = tuple(methods)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._changed = asyncio.Event()
        self._handlers = {method: self._handler(method) for method in self._methods}
        for method, handler in self._handlers.items():
            connection.on(method, handler, session_id=session_id)

    def _handler(self, method: str) -> EventHandler:
        def record(params: dict[str, Any]) -> None:
            self.events.append((method, params))
            self._changed.set()

        return record

    async def wait(
        self, predicate: Callable[[str, dict[str, Any]], bool], *, timeout_s: float, what: str
    ) -> dict[str, Any]:
        """Return the params of the first recorded event matching ``predicate``.

        Raises:
            WebSkrapError: ``timeout`` when no event matches in time.
        """
        deadline = time.monotonic() + timeout_s
        seen = 0
        while True:
            for method, params in self.events[seen:]:
                if predicate(method, params):
                    return params
            seen = len(self.events)
            self._changed.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = f"timed out after {timeout_s * 1000:.0f}ms waiting for {what}"
                raise WebSkrapError(msg, code=ErrorCode.TIMEOUT)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), remaining)

    def stop(self) -> None:
        """Unregister every handler; recorded events stay readable."""
        for method, handler in self._handlers.items():
            self._connection.off(method, handler, session_id=self._session_id)


class CDPPage:
    """One page target attached over a flat session.

    Attributes:
        connection: The browser's :class:`CDPConnection`.
        session_id: Flat session id for this target.
        target_id: The page target id, which is also its main frame id.
    """

    def __init__(self, connection: CDPConnection, session_id: str, target_id: str) -> None:
        """Wrap an attached target; :meth:`CDPBrowser.new_page` calls this."""
        self.connection = connection
        self.session_id = session_id
        self.target_id = target_id
        self.default_timeout_s = 30.0

    async def send(
        self, method: str, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Send ``method`` to this page's session."""
        kwargs.setdefault("timeout_s", self.default_timeout_s)
        return await self.connection.send(method, params, session_id=self.session_id, **kwargs)

    def record(self, *methods: str) -> EventRecorder:
        """Start recording ``methods`` events from this page."""
        return EventRecorder(self.connection, methods, session_id=self.session_id)

    async def evaluate(self, expression: str, arg: Any = None, /) -> Any:
        """Run ``expression`` in a fresh isolated world and return its JSON value.

        Matches Playwright's ``page.evaluate``: when ``expression`` evaluates
        to a function, that function is called with ``arg`` and its result
        (awaited) is returned; otherwise the expression's own value is. See
        :data:`_EVALUATE_SCRIPT`. The world is created per call because a
        navigation destroys it.
        ``Runtime.evaluate`` with an explicit context id works without
        ``Runtime.enable``; the isolated world shares the DOM with the page but
        not its JavaScript globals.

        Raises:
            CDPError: If the script threw or its value is not serializable.
        """
        world = await self.send(
            "Page.createIsolatedWorld",
            {"frameId": self.target_id, "worldName": f"w{uuid4().hex[:8]}"},
        )
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": f"({_EVALUATE_SCRIPT})({json.dumps(expression)}, {json.dumps(arg)})",
                "contextId": world["executionContextId"],
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            description = details.get("exception", {}).get("description") or details.get("text")
            raise CDPError(f"page script failed: {description}")
        return result.get("result", {}).get("value")


@dataclass
class CDPBrowser:
    """A Chromium process launched with ``--remote-debugging-pipe``.

    Closing it asks Chromium to exit, kills it after :data:`CLOSE_TIMEOUT_S`,
    reaps the process, and removes the temporary profile and log it created.
    """

    connection: CDPConnection
    pid: int
    log_path: Path
    temp_dir: Path
    _closed: bool = field(default=False, init=False)

    async def new_page(self) -> CDPPage:
        """Create a blank page target and attach to it with a flat session."""
        target = await self.connection.send("Target.createTarget", {"url": "about:blank"})
        attached = await self.connection.send(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
        )
        return CDPPage(self.connection, attached["sessionId"], target["targetId"])

    async def close(self) -> None:
        """Stop the browser and clean up. Idempotent."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(WebSkrapError):
            await self.connection.send("Browser.close", timeout_s=CLOSE_TIMEOUT_S)
        await self.connection.close()
        await asyncio.to_thread(_reap, self.pid, CLOSE_TIMEOUT_S)
        await asyncio.to_thread(shutil.rmtree, self.temp_dir, True)


def _reap(pid: int, timeout_s: float) -> None:
    """Wait for ``pid`` to exit, killing its process group after ``timeout_s``."""
    if sys.platform == "win32":
        # launch() already refuses Windows; this guard lets type checkers see it.
        raise WebSkrapError(_POSIX_ONLY, code=ErrorCode.BROWSER_LAUNCH)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            done, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if done:
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)


def _high_fd(fd: int) -> int:
    """Return a close-on-exec copy of ``fd`` numbered 10 or above, closing ``fd``.

    ``posix_spawn`` applies dup2 actions in order. Moving every source descriptor
    out of the 0-4 range first means no action overwrites a later one's source,
    and no source already sits on its target, where dup2 would keep close-on-exec.
    """
    if sys.platform == "win32":
        # launch() already refuses Windows; this guard lets type checkers see it.
        raise WebSkrapError(_POSIX_ONLY, code=ErrorCode.BROWSER_LAUNCH)
    # Imported here: fcntl does not exist on Windows, and importing this module
    # must not break the CLI there.
    import fcntl

    moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(fd)
    return moved


def _log_tail(path: Path) -> str:
    try:
        with path.open("rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - _LOG_TAIL_BYTES))
            return log.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _exit_error(log_path: Path) -> WebSkrapError:
    """Describe a browser that closed its pipe, keeping sandbox text for ``classify``."""
    tail = _log_tail(log_path).lower()
    if any(marker in tail for marker in SANDBOX_FAILURE_MARKERS):
        return WebSkrapError(
            "browser exited during startup: no usable sandbox. Enable unprivileged user "
            "namespaces, or set chromium_sandbox=False to accept weaker isolation",
            code=ErrorCode.SANDBOX,
        )
    return WebSkrapError(
        "browser exited during startup or closed the CDP pipe", code=ErrorCode.BROWSER_LAUNCH
    )


async def launch(
    executable: str | Path,
    *,
    args: Iterable[str] = (),
    headless: bool = True,
    user_data_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 30.0,
) -> CDPBrowser:
    """Start Chromium on a debugging pipe and wait until it answers.

    The command line is deliberately short: the pipe, the profile, first-run
    suppression, no testing field trials, ``args``, and ``--headless`` when
    requested. Playwright's long default flag list is not copied, since each
    extra flag is a difference from a real Chrome that a site might measure.

    Args:
        executable: Chromium or Chrome binary.
        args: Extra Chromium flags, already validated by the caller.
        headless: Add ``--headless`` (Chromium's new headless mode).
        user_data_dir: Persistent profile; a temporary one is used when None.
        env: Browser environment; defaults to this process's.
        timeout_s: How long to wait for the first reply.

    Raises:
        WebSkrapError: ``browser_launch`` or ``sandbox`` when Chromium does not
            start or answer in time.
    """
    if sys.platform == "win32":
        raise WebSkrapError(_POSIX_ONLY, code=ErrorCode.BROWSER_LAUNCH)
    executable = Path(executable)
    if not executable.is_file():
        msg = f"Chromium executable is not found at {executable}. Run: webskrap install"
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)

    temp_dir = Path(tempfile.mkdtemp(prefix="webskrap-cdp-"))
    profile = temp_dir / "user-data" if user_data_dir is None else user_data_dir
    profile.mkdir(parents=True, exist_ok=True)
    log_path = temp_dir / "browser.log"
    command = [
        str(executable),
        "--remote-debugging-pipe",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        # Chromium builds (not branded Chrome) apply their testing field
        # trials by default. Stable Chrome never runs that config, and one of
        # its trials trims navigator.languages to a single entry.
        "--disable-field-trial-config",
        *args,
    ]
    if headless:
        command.append("--headless")
    command.append("about:blank")

    to_browser_read, to_browser_write = os.pipe()
    from_browser_read, from_browser_write = os.pipe()
    child_in = _high_fd(to_browser_read)
    child_out = _high_fd(from_browser_write)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    devnull = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
    try:
        # Fixed argv, no shell; dup2 clears close-on-exec on targets 0-4 only,
        # so the browser inherits nothing else from this process.
        pid = os.posix_spawn(
            str(executable),
            command,
            dict(env if env is not None else os.environ),
            file_actions=[
                (os.POSIX_SPAWN_DUP2, devnull, 0),
                (os.POSIX_SPAWN_DUP2, log_fd, 1),
                (os.POSIX_SPAWN_DUP2, log_fd, 2),
                (os.POSIX_SPAWN_DUP2, child_in, 3),
                (os.POSIX_SPAWN_DUP2, child_out, 4),
            ],
            setpgroup=0,
        )
    except BaseException:
        for fd in (to_browser_write, from_browser_read):
            os.close(fd)
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        for fd in (child_in, child_out, log_fd, devnull):
            os.close(fd)

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
    read_file = os.fdopen(from_browser_read, "rb", 0)
    write_file = os.fdopen(to_browser_write, "wb", 0)
    try:
        read_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), read_file
        )
        write_transport, _protocol = await loop.connect_write_pipe(asyncio.Protocol, write_file)
    except BaseException:
        # No connection exists yet, so close() cannot ask Chromium to exit.
        read_file.close()
        write_file.close()
        await asyncio.to_thread(_reap, pid, 0)
        await asyncio.to_thread(shutil.rmtree, temp_dir, True)
        raise
    connection = CDPConnection(reader, write_transport, read_transport)
    connection.on_disconnect = lambda: _exit_error(log_path)
    connection.start()
    browser = CDPBrowser(connection, pid, log_path, temp_dir)
    try:
        await connection.send("Browser.getVersion", timeout_s=timeout_s)
    except BaseException as exc:
        await browser.close()
        if isinstance(exc, WebSkrapError) and exc.code is ErrorCode.TIMEOUT:
            msg = f"browser did not answer on the CDP pipe within {timeout_s:.0f}s"
            raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH) from exc
        raise
    return browser


#: Where Chrome and Edge install on Linux and macOS, per channel.
_CHANNEL_PATHS: dict[str, tuple[str, ...]] = {
    "chrome": (
        "/opt/google/chrome/chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ),
    "msedge": (
        "/opt/microsoft/msedge/msedge",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ),
}


def _playwright_browsers_dirs() -> list[Path]:
    """Return where ``webskrap install`` put browsers, per Playwright's rules.

    ``PLAYWRIGHT_BROWSERS_PATH=0`` means each driver package keeps its own
    ``.local-browsers`` directory. ``find_spec`` locates the packages without
    importing them, so no driver code runs.
    """
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured == "0":
        roots = []
        for package in ("playwright", "patchright"):
            spec = importlib.util.find_spec(package)
            if spec is not None and spec.origin is not None:
                roots.append(Path(spec.origin).parent / "driver" / "package" / ".local-browsers")
        return roots
    if configured:
        return [Path(configured)]
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Caches" / "ms-playwright"]
    return [Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ms-playwright"]


def _revision(path: Path) -> int:
    suffix = path.name.rpartition("-")[2]
    return int(suffix) if suffix.isdigit() else -1


def find_executable(channel: str | None) -> Path:
    """Return the binary for ``channel`` without starting a Playwright driver.

    ``chrome`` and ``msedge`` are looked up at their standard install paths.
    ``None`` and ``chromium`` use the newest Chromium that ``webskrap install``
    downloaded into Playwright's browser cache, which Patchright shares.

    Raises:
        WebSkrapError: ``browser_launch`` when no binary exists; the message
            matches the launch-failure markers channel fallback retries on.
    """
    if channel in _CHANNEL_PATHS:
        for candidate in _CHANNEL_PATHS[channel]:
            if Path(candidate).is_file():
                return Path(candidate)
        msg = f"Chromium distribution '{channel}' is not found at {_CHANNEL_PATHS[channel][0]}"
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
    if channel not in (None, "chromium"):
        msg = f"channel '{channel}' must be one of: chrome, msedge, chromium"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    roots = _playwright_browsers_dirs()
    patterns = (
        "chrome-linux*/chrome",
        "chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        "chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    )
    revision_dirs = [path for root in roots for path in root.glob("chromium-*")]
    for revision_dir in sorted(revision_dirs, key=_revision, reverse=True):
        for pattern in patterns:
            for candidate in revision_dir.glob(pattern):
                if candidate.is_file():
                    return candidate
    searched = ", ".join(str(root) for root in roots) or "no Playwright package"
    msg = (
        f"Chromium is not installed. Executable doesn't exist under {searched}. "
        "Run: webskrap install"
    )
    raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
