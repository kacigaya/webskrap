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
import base64
import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import signal
import struct
import subprocess  # nosec B404  # noqa: S404 - fixed argv for Chromium, never a shell
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any, Literal, Protocol
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
_POSIX_ONLY = (
    "the debugging pipe needs Linux or macOS (posix_spawn with pipe descriptors); "
    "use transport='port'"
)
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


class Transport(Protocol):
    """Carries whole CDP messages; the pipe and a WebSocket both qualify."""

    async def read_message(self) -> bytes:
        """Return the next message; raise ``ConnectionError`` once closed."""
        ...

    def write_message(self, data: bytes) -> None:
        """Queue one message for sending."""
        ...

    async def close(self) -> None:
        """Release the transport. Idempotent."""
        ...


class PipeTransport:
    """Chromium's debugging pipe: NUL-terminated JSON in each direction."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.WriteTransport,
        read_transport: asyncio.BaseTransport | None = None,
    ) -> None:
        """Wrap the reader and writer :func:`launch` connected to fds 3 and 4."""
        self._reader = reader
        self._writer = writer
        self._read_transport = read_transport

    async def read_message(self) -> bytes:
        """Return the next message without its NUL terminator."""
        try:
            raw = await self._reader.readuntil(b"\0")
        except asyncio.IncompleteReadError as exc:
            raise ConnectionError("the browser closed the CDP pipe") from exc
        return raw[:-1]

    def write_message(self, data: bytes) -> None:
        """Write ``data`` followed by the NUL terminator."""
        self._writer.write(data + b"\0")

    async def close(self) -> None:
        """Close both pipe ends."""
        self._writer.close()
        if self._read_transport is not None:
            self._read_transport.close()


#: Fixed by RFC 6455 for the Sec-WebSocket-Accept proof.
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OPCODE_TEXT, _OPCODE_CLOSE, _OPCODE_PING, _OPCODE_PONG = 0x1, 0x8, 0x9, 0xA


def _mask(payload: bytes, key: bytes) -> bytes:
    """XOR ``payload`` with the 4-byte ``key``, as RFC 6455 masking requires."""
    if not payload:
        return payload
    repeated = (key * (len(payload) // 4 + 1))[: len(payload)]
    value = int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")
    return value.to_bytes(len(payload), "big")


class WebSocketTransport:
    """A minimal RFC 6455 client for a loopback DevTools endpoint.

    Persistent sessions need it: each CLI command is a new process, and only
    the browser's debugging port can be reattached, not its pipe. Text frames
    carry the JSON messages; fragments are joined, pings answered, and a
    close frame or EOF ends the stream. No ``Origin`` header is sent, which
    Chromium accepts without ``--remote-allow-origins``.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Wrap a stream that has completed the upgrade; see :meth:`connect`."""
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(
        cls, host: str, port: int, path: str, *, timeout_s: float = 10.0
    ) -> WebSocketTransport:
        """Open ``ws://host:port/path`` and verify the server's handshake.

        Raises:
            ConnectionError: If the server refuses the upgrade or answers with
                a wrong ``Sec-WebSocket-Accept``.
            OSError: If nothing listens on the port.
        """
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=MAX_MESSAGE_BYTES), timeout_s
        )
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout_s)
            lines = head.decode("latin-1").split("\r\n")
            headers = {
                name.strip().lower(): value.strip()
                for name, _, value in (line.partition(":") for line in lines[1:] if line)
            }
            # SHA-1 is what RFC 6455 specifies for this proof; it is not a
            # security boundary here.
            digest = hashlib.sha1((key + _WEBSOCKET_GUID).encode(), usedforsecurity=False)
            expected = base64.b64encode(digest.digest()).decode()
            if not lines[0].startswith("HTTP/1.1 101"):
                raise ConnectionError(f"DevTools refused the WebSocket upgrade: {lines[0]}")
            if headers.get("sec-websocket-accept") != expected:
                raise ConnectionError("DevTools answered the upgrade with a wrong accept key")
        except BaseException:
            writer.close()
            raise
        return cls(reader, writer)

    async def read_message(self) -> bytes:
        """Return the next complete message, answering pings on the way."""
        parts: list[bytes] = []
        size = 0
        while True:
            try:
                first, second = await self._reader.readexactly(2)
                length = second & 0x7F
                if length == 126:
                    (length,) = struct.unpack(">H", await self._reader.readexactly(2))
                elif length == 127:
                    (length,) = struct.unpack(">Q", await self._reader.readexactly(8))
                key = await self._reader.readexactly(4) if second & 0x80 else b""
                if size + length > MAX_MESSAGE_BYTES:
                    raise ConnectionError("DevTools sent a message over the size limit")
                payload = await self._reader.readexactly(length)
            except asyncio.IncompleteReadError as exc:
                raise ConnectionError("DevTools closed the WebSocket") from exc
            if key:
                payload = _mask(payload, key)
            opcode = first & 0x0F
            if opcode == _OPCODE_CLOSE:
                raise ConnectionError("DevTools closed the WebSocket")
            if opcode == _OPCODE_PING:
                self._send_frame(_OPCODE_PONG, payload)
                continue
            if opcode == _OPCODE_PONG:
                continue
            parts.append(payload)
            size += length
            if first & 0x80:
                return b"".join(parts)

    def write_message(self, data: bytes) -> None:
        """Send ``data`` as one text frame."""
        self._send_frame(_OPCODE_TEXT, data)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        # Clients must mask every frame (RFC 6455 section 5.3).
        key = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = struct.pack(">BB", 0x80 | opcode, 0x80 | length)
        elif length < 1 << 16:
            header = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, length)
        self._writer.write(header + key + _mask(payload, key))

    async def close(self) -> None:
        """Send a close frame and close the socket. Idempotent."""
        if self._writer.is_closing():
            return
        with contextlib.suppress(ConnectionError, RuntimeError):
            self._send_frame(_OPCODE_CLOSE, b"")
        self._writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await self._writer.wait_closed()


class CDPConnection:
    """Routes commands, replies and events over one :class:`Transport`.

    Commands get increasing ids and resolve when the matching reply arrives.
    Events go to handlers registered per ``(session_id, method)``; the browser
    target uses session id None. When the transport closes, every pending
    command fails with the error given to :meth:`close`.
    """

    def __init__(self, transport: Transport) -> None:
        """Wrap an open transport; call :meth:`start` to begin reading."""
        self._transport = transport
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
            self._transport.write_message(json.dumps(message).encode())
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
        await self._transport.close()
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
                self._dispatch(json.loads(await self._transport.read_message()))
        except ConnectionError:
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
        if method == "Target.detachedFromTarget":
            # A detached session sends nothing more; drop its handlers, or a
            # long-lived browser keeps every closed page's handlers alive.
            gone = params.get("sessionId")
            for key in [key for key in self._handlers if key[0] == gone]:
                del self._handlers[key]
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


#: CDP lifecycle event name for each Playwright load state.
LIFECYCLE_EVENTS: dict[str, str] = {
    "domcontentloaded": "DOMContentLoaded",
    "load": "load",
    "networkidle": "networkIdle",
}


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
        self._events_enabled = False
        self._shared_world: int | None = None
        #: Out-of-process iframes auto-attached to this page, by target id;
        #: see :func:`webskrap.cdp_dom.frames`. None until auto-attach is on.
        self.iframe_targets: dict[str, dict[str, Any]] | None = None

    async def send(
        self, method: str, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Send ``method`` to this page's session."""
        kwargs.setdefault("timeout_s", self.default_timeout_s)
        return await self.connection.send(method, params, session_id=self.session_id, **kwargs)

    async def enable_events(self) -> None:
        """Turn on the page, lifecycle and network events :meth:`goto` reads.

        Idempotent per page object. These domains are what Playwright and
        Patchright enable too; ``Runtime`` stays off.
        """
        if self._events_enabled:
            return
        await self.send("Page.enable")
        await self.send("Page.setLifecycleEventsEnabled", {"enabled": True})
        await self.send("Network.enable")
        self._events_enabled = True

    async def goto(self, url: str, *, wait_until: str, timeout_s: float) -> dict[str, Any]:
        """Navigate and wait for ``wait_until``; return the document response.

        Navigation and the load wait share one ``timeout_s`` budget, as in
        Playwright's ``goto``. The returned CDP ``Response`` object is empty
        for loads without a network response (``about:``, ``data:``, same
        document).

        Raises:
            WebSkrapError: ``navigation`` for a network error, ``timeout``
                when the load state is not reached in time.
        """
        await self.enable_events()
        events = self.record("Page.lifecycleEvent", "Network.responseReceived")
        deadline = time.monotonic() + timeout_s
        try:
            navigation = await self.send("Page.navigate", {"url": url}, timeout_s=timeout_s)
            if navigation.get("errorText"):
                msg = f"{navigation['errorText']} at {url}"
                raise WebSkrapError(msg, code=ErrorCode.NAVIGATION)
            loader_id = navigation.get("loaderId")
            if not loader_id:
                return {}
            await self.wait_lifecycle(events, loader_id, wait_until, deadline, url)
            try:
                document = await events.wait(
                    lambda method, params: (
                        method == "Network.responseReceived"
                        and params.get("loaderId") == loader_id
                        and params.get("type") == "Document"
                    ),
                    # The response usually arrives before navigate returns.
                    timeout_s=min(5.0, max(0.0, deadline - time.monotonic())),
                    what="the document response",
                )
            except WebSkrapError as exc:
                if exc.code is not ErrorCode.TIMEOUT:
                    raise
                return {}
            return document.get("response", {})
        finally:
            events.stop()

    async def wait_lifecycle(
        self,
        events: EventRecorder,
        loader_id: str | None,
        wait_until: str,
        deadline: float,
        url: str,
    ) -> None:
        """Wait for ``wait_until`` in the main frame, on ``loader_id`` or any loader.

        ``events`` must have been recording ``Page.lifecycleEvent`` since
        before the navigation started.
        """
        name = LIFECYCLE_EVENTS.get(wait_until)
        if name is None:
            return
        await events.wait(
            lambda method, params: (
                method == "Page.lifecycleEvent"
                and params.get("frameId") == self.target_id
                and (loader_id is None or params.get("loaderId") == loader_id)
                and params.get("name") == name
            ),
            timeout_s=max(0.0, deadline - time.monotonic()),
            what=f"{wait_until} on {url}",
        )

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        """Return a PNG of the viewport, or of the whole page when ``full_page``."""
        params: dict[str, Any] = {"format": "png"}
        if full_page:
            metrics = await self.send("Page.getLayoutMetrics")
            size = metrics.get("cssContentSize") or metrics["contentSize"]
            params["captureBeyondViewport"] = True
            params["clip"] = {
                "x": 0,
                "y": 0,
                "width": size["width"],
                "height": size["height"],
                "scale": 1,
            }
        shot = await self.send("Page.captureScreenshot", params)
        return base64.b64decode(shot["data"])

    def record(self, *methods: str) -> EventRecorder:
        """Start recording ``methods`` events from this page."""
        return EventRecorder(self.connection, methods, session_id=self.session_id)

    async def isolated_world(self) -> int:
        """Create a fresh isolated world in the main frame and return its context id.

        It shares the DOM with the page but not its JavaScript globals, and
        needs no ``Runtime.enable``. A navigation destroys it, so callers
        create one per operation.
        """
        world = await self.send(
            "Page.createIsolatedWorld",
            {"frameId": self.target_id, "worldName": f"w{uuid4().hex[:8]}"},
        )
        return int(world["executionContextId"])

    async def shared_world(self) -> int:
        """Return an isolated world kept for this page object's lookups.

        Polling loops reuse it instead of creating one world per poll. A
        navigation destroys it; :meth:`forget_world` drops the cached id after
        Chromium reports the context gone, and the next call makes a new one.
        """
        if self._shared_world is None:
            self._shared_world = await self.isolated_world()
        return self._shared_world

    def forget_world(self) -> None:
        """Drop the cached :meth:`shared_world` id."""
        self._shared_world = None

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
        world = await self.isolated_world()
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": f"({_EVALUATE_SCRIPT})({json.dumps(expression)}, {json.dumps(arg)})",
                "contextId": world,
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
    """A Chromium process WebSkrap launched, on a pipe or a loopback port.

    Closing it asks Chromium to exit, kills it after :data:`CLOSE_TIMEOUT_S`,
    reaps the process, and removes the temporary profile and log it created.
    ``process`` is set for a port launch (:func:`launch` with
    ``transport="port"``); a pipe launch is tracked by ``pid`` alone.
    """

    connection: CDPConnection
    pid: int
    log_path: Path
    temp_dir: Path
    process: subprocess.Popen[bytes] | None = None
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
        if self.process is not None:
            await asyncio.to_thread(_stop_process, self.process, CLOSE_TIMEOUT_S)
        else:
            await asyncio.to_thread(_reap, self.pid, CLOSE_TIMEOUT_S)
        await asyncio.to_thread(shutil.rmtree, self.temp_dir, True)


def _stop_process(process: subprocess.Popen[bytes], timeout_s: float) -> None:
    """Wait for a port-launched browser to exit, then kill its process tree.

    Chromium's renderers are child processes; killing only the browser
    process would leave them holding the profile. Windows has no process
    groups, so ``taskkill /T`` stops the tree there.
    """
    try:
        process.wait(timeout=timeout_s)
        return
    except subprocess.TimeoutExpired:
        pass
    if sys.platform == "win32":
        taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe")
        # Fixed argv, no shell; the PID is this process's own child.
        subprocess.run(  # nosec B603  # noqa: S603
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=timeout_s)


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
    transport: Literal["pipe", "port"] | None = None,
) -> CDPBrowser:
    """Start Chromium and wait until it answers over CDP.

    The command line is deliberately short: the debugging channel, the
    profile, first-run suppression, no testing field trials, ``args``, and
    ``--headless`` when requested. Playwright's long default flag list is not
    copied, since each extra flag is a difference from a real Chrome that a
    site might measure.

    Args:
        executable: Chromium or Chrome binary.
        args: Extra Chromium flags, already validated by the caller.
        headless: Add ``--headless`` (Chromium's new headless mode).
        user_data_dir: Persistent profile; a temporary one is used when None.
        env: Browser environment; defaults to this process's.
        timeout_s: How long to wait for the first reply.
        transport: ``pipe`` (the debugging pipe; no port is opened) or
            ``port`` (a loopback debugging port and the WebSocket client).
            None picks ``port`` on Windows, where :func:`os.posix_spawn`
            cannot map the pipe descriptors, and ``pipe`` elsewhere.

    Raises:
        WebSkrapError: ``browser_launch`` or ``sandbox`` when Chromium does not
            start or answer in time.
    """
    if transport is None:
        transport = "port" if sys.platform == "win32" else "pipe"
    executable = Path(executable)
    if not executable.is_file():
        msg = f"Chromium executable is not found at {executable}. Run: webskrap install"
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)

    temp_dir = Path(tempfile.mkdtemp(prefix="webskrap-cdp-"))
    profile = temp_dir / "user-data" if user_data_dir is None else user_data_dir
    profile.mkdir(parents=True, exist_ok=True)
    log_path = temp_dir / "browser.log"
    channel_flag = "--remote-debugging-pipe" if transport == "pipe" else "--remote-debugging-port=0"
    command = [
        str(executable),
        channel_flag,
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
    environment = dict(env if env is not None else os.environ)
    if transport == "port":
        browser = await _launch_on_port(
            command, environment, profile, log_path, temp_dir, timeout_s
        )
    else:
        browser = await _launch_on_pipe(command, environment, log_path, temp_dir)
    try:
        await browser.connection.send("Browser.getVersion", timeout_s=timeout_s)
    except BaseException as exc:
        await browser.close()
        if isinstance(exc, WebSkrapError) and exc.code is ErrorCode.TIMEOUT:
            msg = f"browser did not answer over CDP within {timeout_s:.0f}s"
            raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH) from exc
        raise
    return browser


async def _launch_on_pipe(
    command: list[str], env: dict[str, str], log_path: Path, temp_dir: Path
) -> CDPBrowser:
    """Spawn Chromium with fds 3 and 4 mapped to a pipe pair (POSIX only)."""
    if sys.platform == "win32":
        raise WebSkrapError(_POSIX_ONLY, code=ErrorCode.BROWSER_LAUNCH)
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
            command[0],
            command,
            env,
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
    connection = CDPConnection(PipeTransport(reader, write_transport, read_transport))
    connection.on_disconnect = lambda: _exit_error(log_path)
    connection.start()
    return CDPBrowser(connection, pid, log_path, temp_dir)


async def _launch_on_port(
    command: list[str],
    env: dict[str, str],
    profile: Path,
    log_path: Path,
    temp_dir: Path,
    timeout_s: float,
) -> CDPBrowser:
    """Start Chromium on a loopback debugging port and attach over WebSocket.

    Chromium picks a free port and writes it, with the browser target's
    random path, to ``DevToolsActivePort`` in the profile. The port listens
    on 127.0.0.1 only, and only for as long as the browser runs.
    """
    port_file = profile / "DevToolsActivePort"
    port_file.unlink(missing_ok=True)
    # Its own process group (Windows) or session (POSIX): a Ctrl+C in this
    # console does not reach the browser, and close() can stop the whole tree.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    with log_path.open("wb") as log:
        # Fixed argv, no shell; the executable comes from find_executable or
        # the caller, and every flag is a constant or a validated launch arg.
        process = subprocess.Popen(  # nosec B603  # noqa: S603
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            creationflags=flags,
            start_new_session=sys.platform != "win32",
        )
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            if process.poll() is not None:
                raise _exit_error(log_path)
            try:
                lines = port_file.read_text(encoding="utf-8").split()
            except OSError:
                lines = []
            if len(lines) >= 2 and lines[0].isdigit():
                break
            if time.monotonic() >= deadline:
                msg = f"browser did not report a DevTools port within {timeout_s:.0f}s"
                raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
            await asyncio.sleep(0.05)
        transport = await WebSocketTransport.connect(
            "127.0.0.1", int(lines[0]), lines[1], timeout_s=timeout_s
        )
    except BaseException:
        await asyncio.to_thread(_stop_process, process, 0)
        await asyncio.to_thread(shutil.rmtree, temp_dir, True)
        raise
    connection = CDPConnection(transport)
    connection.on_disconnect = lambda: _exit_error(log_path)
    connection.start()
    return CDPBrowser(connection, process.pid, log_path, temp_dir, process)


def _channel_paths(channel: str) -> list[Path]:
    """Return where ``chrome`` or ``msedge`` installs on this platform."""
    if sys.platform == "win32":
        bases = [
            os.environ.get(name) for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        ]
        folder = ("Google", "Chrome") if channel == "chrome" else ("Microsoft", "Edge")
        binary = "chrome.exe" if channel == "chrome" else "msedge.exe"
        return [Path(base, *folder, "Application", binary) for base in bases if base]
    if sys.platform == "darwin":
        app = "Google Chrome" if channel == "chrome" else "Microsoft Edge"
        return [Path(f"/Applications/{app}.app/Contents/MacOS/{app}")]
    if channel == "chrome":
        return [Path("/opt/google/chrome/chrome")]
    return [Path("/opt/microsoft/msedge/msedge")]


#: Distribution Chromium builds, used when nothing was downloaded.
_SYSTEM_CHROMIUM = (
    Path("/usr/bin/chromium"),
    Path("/usr/bin/chromium-browser"),
    Path("/snap/bin/chromium"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
)


def _playwright_browsers_dirs() -> list[Path]:
    """Return where WebSkrap 2.x had Playwright put browsers, per Playwright's rules.

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
    """Return the browser binary for ``channel``.

    ``chrome`` and ``msedge`` are looked up at their standard install paths.
    ``None`` and ``chromium`` use, in order: the newest Chrome for Testing
    that ``webskrap install`` downloaded, a Chromium that Playwright or
    Patchright downloaded for WebSkrap 2.x, then a distribution Chromium.

    Raises:
        WebSkrapError: ``browser_launch`` when no binary exists; the message
            matches the launch-failure markers channel fallback retries on.
    """
    if channel in ("chrome", "msedge"):
        candidates = _channel_paths(channel)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        where = candidates[0] if candidates else channel
        msg = f"Chromium distribution '{channel}' is not found at {where}"
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
    if channel not in (None, "chromium"):
        msg = f"channel '{channel}' must be one of: chrome, msedge, chromium"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    # Imported here: the installer is only needed for the downloaded browser.
    from webskrap.install import browsers_dir, installed_executables

    downloaded = installed_executables()
    if downloaded:
        return downloaded[0]
    roots = _playwright_browsers_dirs()
    patterns = (
        "chrome-linux*/chrome",
        "chrome-win/chrome.exe",
        "chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        "chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    )
    revision_dirs = [path for root in roots for path in root.glob("chromium-*")]
    for revision_dir in sorted(revision_dirs, key=_revision, reverse=True):
        for pattern in patterns:
            for candidate in revision_dir.glob(pattern):
                if candidate.is_file():
                    return candidate
    for candidate in _SYSTEM_CHROMIUM:
        if candidate.is_file():
            return candidate
    msg = (
        f"Chromium is not installed. Executable doesn't exist under {browsers_dir()}. "
        "Run: webskrap install"
    )
    raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
