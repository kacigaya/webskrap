"""CDP connections, events, and page commands."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from itertools import count
from typing import Any
from uuid import uuid4

from webskrap.driver.transport import Transport as Transport
from webskrap.errors import ErrorCode, WebSkrapError

logger = logging.getLogger("webskrap.cdp")

#: Callback for one CDP event; receives the event's ``params``.
EventHandler = Callable[[dict[str, Any]], None]

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

    async def wait_closed(self) -> None:
        """Return when the browser side closes the transport."""
        if self._read_task is not None:
            await asyncio.shield(self._read_task)

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
        pointer: Where this page's mouse last went, in viewport CSS pixels,
            or None before the first mouse event; see
            :func:`webskrap.cdp_dom.travel`.
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
        self.pointer: tuple[float, float] | None = None

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
        document). A main-frame document that replaces itself before reaching
        ``wait_until`` (a script reload or redirect) is followed, and the
        response is the replacement's.

        Raises:
            WebSkrapError: ``navigation`` for a network error, ``timeout``
                when the load state is not reached in time.
        """
        await self.enable_events()
        events = self.record(
            "Page.lifecycleEvent", "Page.frameNavigated", "Network.responseReceived"
        )
        deadline = time.monotonic() + timeout_s
        try:
            navigation = await self.send("Page.navigate", {"url": url}, timeout_s=timeout_s)
            # Empty HTTP error bodies can commit Chromium's own error document.
            # They still have a real response, unlike DNS or TLS failures.
            http_error = navigation.get("errorText") == "net::ERR_HTTP_RESPONSE_CODE_FAILURE"
            if navigation.get("errorText") and not http_error:
                msg = f"{navigation['errorText']} at {url}"
                raise WebSkrapError(msg, code=ErrorCode.NAVIGATION)
            loader_id = navigation.get("loaderId")
            if not loader_id:
                if http_error:
                    raise WebSkrapError(
                        f"{navigation['errorText']} at {url}", code=ErrorCode.NAVIGATION
                    )
                return {}
            loader_id = await self.wait_lifecycle(events, loader_id, wait_until, deadline, url)
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
                if http_error:
                    raise WebSkrapError(
                        f"{navigation['errorText']} at {url}", code=ErrorCode.NAVIGATION
                    ) from exc
                return {}
            response = document.get("response", {})
            if http_error and not 400 <= response.get("status", 0) <= 599:
                raise WebSkrapError(
                    f"{navigation['errorText']} at {url}", code=ErrorCode.NAVIGATION
                )
            return response
        finally:
            events.stop()

    async def wait_lifecycle(
        self,
        events: EventRecorder,
        loader_id: str | None,
        wait_until: str,
        deadline: float,
        url: str,
    ) -> str | None:
        """Wait for ``wait_until`` in the main frame, on ``loader_id`` or any loader.

        ``events`` must have been recording ``Page.lifecycleEvent`` since
        before the navigation started. When it also records
        ``Page.frameNavigated``, a main-frame document that commits after
        ``loader_id`` counts too: a page that reloads itself before its first
        document reaches ``wait_until`` would otherwise never get there.
        Documents recorded before ``loader_id`` committed never count.

        Returns:
            The loader that reached ``wait_until``.
        """
        name = LIFECYCLE_EVENTS.get(wait_until)
        if name is None:
            return loader_id
        loaders = {loader_id}
        committed = False

        def reached(method: str, params: dict[str, Any]) -> bool:
            nonlocal committed
            if method == "Page.frameNavigated":
                frame = params.get("frame", {})
                if loader_id is not None and frame.get("id") == self.target_id:
                    if frame.get("loaderId") == loader_id:
                        committed = True
                    elif committed:
                        loaders.add(frame.get("loaderId"))
                return False
            return (
                method == "Page.lifecycleEvent"
                and params.get("frameId") == self.target_id
                and (loader_id is None or params.get("loaderId") in loaders)
                and params.get("name") == name
            )

        params = await events.wait(
            reached,
            timeout_s=max(0.0, deadline - time.monotonic()),
            what=f"{wait_until} on {url}",
        )
        return params.get("loaderId", loader_id)

    async def screenshot(
        self, *, full_page: bool = False, clip: dict[str, float] | None = None
    ) -> bytes:
        """Return a PNG of the viewport, the whole page, or a ``clip`` of it.

        ``clip`` is ``{"x", "y", "width", "height"}`` in CSS pixels from the
        document's top-left; it may extend past the viewport.
        """
        params: dict[str, Any] = {"format": "png"}
        if clip is not None:
            params["captureBeyondViewport"] = True
            params["clip"] = {**clip, "scale": 1}
        elif full_page:
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
