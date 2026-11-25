"""Serve a live view of a browser session on loopback."""

from __future__ import annotations

import asyncio
import base64
import math
import secrets
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic
from typing import Any, TypeVar
from urllib.parse import parse_qs

from webskrap.browser import browser_session
from webskrap.driver import cdp_session, keyboard, targets
from webskrap.driver.cdp import CDPPage
from webskrap.errors import ErrorCode, WebSkrapError

LIFETIME_S = 900
FRAME_TIMEOUT_S = 5
FRAME_WAIT_S = 2
_FRAME_PACE_S = 1 / 30
_JPEG_QUALITY = 90
_MOTION_FRAMES = 2
_MOTION_WINDOW_S = 0.3
_SETTLE_S = 0.25
_TAB_CHECK_S = 0.5
_IDLE_S = 5
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_T = TypeVar("_T")
_CONTROL_KEYS = {
    "Enter",
    "Backspace",
    "Delete",
    "Tab",
    "Home",
    "End",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "PageUp",
    "PageDown",
}

_HTML = b"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WebSkrap Live View</title>
<style>
body { margin: 0; background: #171717; color: #fafafa; font: 1rem system-ui; }
main { position: fixed; inset: 0; display: grid; place-items: center; }
main:focus-visible { outline: 2px solid #fafafa; outline-offset: -3px; }
p { position: fixed; bottom: 0; margin: 1rem; padding: .5rem; background: #171717; }
</style>
<main aria-label="WebSkrap browser preview" data-scroll="false" data-control="false">
<canvas hidden role="img" aria-label="Live viewport of the WebSkrap browser"></canvas>
<p role="status">Connecting to WebSkrap...</p>
</main>
<script>
const token = location.hash.slice(1);
const status = document.querySelector('p');
const image = document.querySelector('canvas');
const context = image.getContext('2d');
const panel = document.querySelector('main');
const headers = {Authorization: 'Bearer ' + token};
let latest, target, frame = 0, painting = 0;
const control = panel.dataset.control === 'true';
const keys = new Set([
  'Enter', 'Backspace', 'Delete', 'Tab', 'Home', 'End',
  'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'PageUp', 'PageDown',
]);

if (control) {
  let queue = Promise.resolve();
  panel.setAttribute('aria-label', 'Browser preview. Click to focus. Press Escape to exit.');
  function send(path, body) {
    const frameTarget = target;
    if (!frameTarget) return;
    queue = queue.then(async () => {
      if (frameTarget !== target) return;
      await fetch(path, {
        method: 'POST', headers: {...headers, 'X-WebSkrap-Target': frameTarget}, body,
        signal: AbortSignal.timeout(7000),
      });
    }).catch(() => {});
  }
  image.addEventListener('click', event => {
    const box = image.getBoundingClientRect();
    const x = (event.clientX - box.left) / box.width;
    const y = (event.clientY - box.top) / box.height;
    if (!(x >= 0 && x < 1 && y >= 0 && y < 1)) return;
    panel.focus({preventScroll: true});
    send('./click?' + new URLSearchParams({x, y}));
  });
  panel.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      panel.blur();
      return;
    }
    if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey ||
        (event.shiftKey && event.key === 'Tab')) return;
    if (!keys.has(event.key) && Array.from(event.key).length !== 1) return;
    event.preventDefault();
    send('./key', event.key);
  });
}

if (panel.dataset.scroll === 'true') {
  panel.tabIndex = 0;
  if (!control) panel.setAttribute('aria-label', 'Browser preview. Scroll with the wheel or keys.');
  let dx = 0, dy = 0, x = 0.5, y = 0.5, sending = false;
  async function scroll() {
    if (sending) return;
    sending = true;
    try {
      while (dx || dy) {
        const query = new URLSearchParams({
          x, y,
          dx: Math.round(Math.max(-2400, Math.min(2400, dx))),
          dy: Math.round(Math.max(-2400, Math.min(2400, dy))),
        });
        dx = dy = 0;
        const response = await fetch('./scroll?' + query, {
          method: 'POST', headers, signal: AbortSignal.timeout(7000),
        });
        if (!response.ok) break;
      }
    } catch {
      dx = dy = 0;
    } finally {
      sending = false;
    }
  }
  panel.addEventListener('wheel', event => {
    if (image.hidden || event.ctrlKey) return;
    event.preventDefault();
    const rect = image.getBoundingClientRect();
    const clamp = value => Math.max(0, Math.min(0.9999, value));
    x = clamp((event.clientX - rect.left) / rect.width);
    y = clamp((event.clientY - rect.top) / rect.height);
    const scale = event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? rect.height : 1;
    dx += event.deltaX * scale;
    dy += event.deltaY * scale;
    void scroll();
  }, {passive: false});
  panel.addEventListener('keydown', event => {
    if (control || image.hidden || event.ctrlKey || event.metaKey || event.altKey) return;
    const steps = {ArrowUp: -80, ArrowDown: 80, PageUp: -600, PageDown: 600,
                   ' ': event.shiftKey ? -600 : 600};
    if (!Object.hasOwn(steps, event.key)) return;
    event.preventDefault();
    x = y = 0.5;
    dy += steps[event.key];
    void scroll();
  });
}

async function paint({blob, size}) {
  const id = ++painting;
  const full = await createImageBitmap(blob);
  const [cssWidth, cssHeight] = size || [full.width, full.height];
  const scale = Math.min(1, innerWidth / cssWidth, innerHeight / cssHeight);
  const width = cssWidth * scale, height = cssHeight * scale;
  const pixels = Math.round(width * devicePixelRatio);
  const bitmap = pixels < full.width ? await createImageBitmap(full, {
    resizeWidth: pixels,
    resizeHeight: Math.round(height * devicePixelRatio),
    resizeQuality: 'high',
  }) : full;
  if (bitmap !== full) full.close();
  if (id === painting) {
    image.width = bitmap.width;
    image.height = bitmap.height;
    image.style.width = width + 'px';
    image.style.height = height + 'px';
    context.drawImage(bitmap, 0, 0);
  }
  bitmap.close();
}

addEventListener('resize', () => {
  if (latest) paint(latest).catch(() => {});
});

async function update() {
  if (!token) {
    status.textContent = 'Open the full viewer link from WebSkrap.';
    return;
  }
  try {
    const response = await fetch('./frame?after=' + frame, {
      headers,
      signal: AbortSignal.timeout(9000),
    });
    if (response.status === 404 || response.status === 410) {
      status.textContent = 'This viewer link has expired or is invalid.';
      status.hidden = false;
      image.hidden = true;
      return;
    }
    if (!response.ok) throw new Error();
    if (response.status === 200) {
      const size = (response.headers.get('X-WebSkrap-Size') || '').split('x').map(Number);
      const known = size.length === 2 && size.every(n => n > 0);
      latest = {blob: await response.blob(), size: known ? size : null};
      const nextTarget = response.headers.get('X-WebSkrap-Target');
      frame = Number(response.headers.get('X-WebSkrap-Frame'));
      if (target !== nextTarget) target = null;
      await paint(latest);
      target = nextTarget;
      image.hidden = false;
      status.hidden = true;
    }
  } catch {
    status.textContent = 'Live view disconnected. Open a new link if this one expired.';
    status.hidden = false;
    setTimeout(update, 500);
    return;
  }
  void update();
}
update();
</script>
</html>
"""


class _Feed:
    """Newest screencast frame of the watched tab, for long-polling viewers."""

    def __init__(self) -> None:
        self.page: CDPPage | None = None
        self.frame = b""
        self.target = ""
        self.size = ""
        self.number = 0
        self.gone = False
        self.polled = -math.inf
        self._fresh = asyncio.Event()
        self._demand = asyncio.Event()

    def publish(self, frame: bytes, target: str, size: str) -> None:
        self.frame, self.target, self.size = frame, target, size
        self.number += 1
        self.wake()

    def wake(self) -> None:
        fresh, self._fresh = self._fresh, asyncio.Event()
        fresh.set()

    def idle(self) -> bool:
        return asyncio.get_running_loop().time() - self.polled > _IDLE_S

    async def watched(self) -> None:
        """Return once a viewer polls again."""
        while self.idle():
            self._demand.clear()
            await self._demand.wait()

    async def after(self, number: int) -> tuple[bytes, str, int, str]:
        """Wait briefly for a frame newer than ``number``; an empty frame means none came."""
        self.polled = asyncio.get_running_loop().time()
        self._demand.set()
        if self.number <= number and not self.gone:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._fresh.wait(), FRAME_WAIT_S)
        if self.gone:
            raise WebSkrapError("The watched session closed", code=ErrorCode.NO_SESSION)
        if self.page is None:
            raise WebSkrapError("The live view is reconnecting")
        if self.number <= number:
            return b"", self.target, self.number, self.size
        return self.frame, self.target, self.number, self.size


async def _cast(page: CDPPage, directory: Path, feed: _Feed, check: Callable[[], None]) -> None:
    """Stream ``page`` into ``feed`` until the session's current tab changes.

    Motion streams JPEG; once the page settles, a lossless PNG replaces it.
    Restarting the screencast makes Chromium send the current view at once.
    """
    loop = asyncio.get_running_loop()
    recent: deque[float] = deque()
    tasks: set[asyncio.Task[None]] = set()
    restarts = asyncio.Lock()
    lossless = True
    paused = False
    settle: asyncio.TimerHandle | None = None

    def spawn(coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def restart(png: bool) -> None:
        params = {"format": "png"} if png else {"format": "jpeg", "quality": _JPEG_QUALITY}
        async with restarts:
            with suppress(WebSkrapError):
                await page.send("Page.stopScreencast")
            with suppress(WebSkrapError):
                await page.send("Page.startScreencast", params)

    async def ack(session_id: int) -> None:
        nonlocal paused
        await asyncio.sleep(_FRAME_PACE_S)
        if feed.idle():
            # Without a viewer, leave the frame unacked so Chromium stops encoding.
            if paused:
                return
            paused = True
            await feed.watched()
            paused = False
            await restart(lossless)
            return
        with suppress(WebSkrapError):
            await page.send("Page.screencastFrameAck", {"sessionId": session_id})

    def settled() -> None:
        nonlocal lossless
        if not lossless and not feed.idle():
            lossless = True
            recent.clear()
            spawn(restart(True))

    def on_frame(params: dict[str, Any]) -> None:
        nonlocal lossless, settle
        metadata = params.get("metadata", {})
        size = ""
        if "deviceWidth" in metadata and "deviceHeight" in metadata:
            size = f"{round(metadata['deviceWidth'])}x{round(metadata['deviceHeight'])}"
        feed.publish(base64.b64decode(params["data"]), page.target_id, size)
        spawn(ack(params["sessionId"]))
        if feed.idle():
            # A restart would act as an ack and keep an unwatched stream running.
            return
        now = loop.time()
        recent.append(now)
        while recent[0] < now - _MOTION_WINDOW_S:
            recent.popleft()
        if lossless and len(recent) >= _MOTION_FRAMES:
            lossless = False
            spawn(restart(False))
        if settle is not None:
            settle.cancel()
        settle = loop.call_later(_SETTLE_S, settled)

    order = directory / targets.PAGE_ORDER_FILE
    page.connection.on("Page.screencastFrame", on_frame, session_id=page.session_id)
    feed.page = page
    try:
        await restart(True)
        while True:
            await asyncio.sleep(_TAB_CHECK_S)
            check()
            infos = (await page.connection.send("Target.getTargets"))["targetInfos"]
            ids = [info["targetId"] for info in infos if info["type"] == "page"]
            pages = targets._ordered_pages(order, ids, remember=False)
            if targets.current_page(directory, pages) != page.target_id:
                return
    finally:
        feed.page = None
        page.connection.off("Page.screencastFrame", on_frame, session_id=page.session_id)
        if settle is not None:
            settle.cancel()
        for task in tasks:
            task.cancel()


async def _follow(
    session: str, directory: Path, port: int, feed: _Feed, check: Callable[[], None]
) -> None:
    """Keep ``feed`` on the session's current tab until the session closes or is replaced."""
    try:
        while True:
            try:
                check()
                async with targets.attach(
                    session,
                    directory / "user-data",
                    port,
                    timeout_ms=FRAME_TIMEOUT_S * 1000,
                    read_only=True,
                ) as page:
                    await _cast(page, directory, feed, check)
            except WebSkrapError as exc:
                if exc.code is ErrorCode.NO_SESSION:
                    return
            await asyncio.sleep(_TAB_CHECK_S)
    finally:
        feed.gone = True
        feed.wake()


async def _serve(
    session: str, ready: asyncio.Future[str], *, allow_scroll: bool, allow_control: bool
) -> None:
    """Publish the viewer URL and serve it until expiry or cancellation."""
    directory, state = browser_session._open_state(session)
    loop = asyncio.get_running_loop()
    deadline = monotonic() + LIFETIME_S
    authorization = f"Bearer {secrets.token_urlsafe(32)}"

    allow_scroll = allow_scroll or allow_control
    html = _HTML.replace(b'data-scroll="false"', b'data-scroll="true"') if allow_scroll else _HTML
    if allow_control:
        html = html.replace(b'data-control="false"', b'data-control="true"')

    feed = _Feed()

    def check() -> None:
        _, current = browser_session._open_state(session)
        if (current["pid"], current["port"]) != (state["pid"], state["port"]):
            raise WebSkrapError("The watched session was replaced", code=ErrorCode.NO_SESSION)

    async def run(action: Callable[[CDPPage], Awaitable[None]], expected_target: str | None) -> str:
        check()
        page = feed.page
        if page is None:
            raise WebSkrapError("The live view is reconnecting")
        if expected_target is not None and page.target_id != expected_target:
            raise WebSkrapError("The watched tab changed", code=ErrorCode.STALE_REF)
        await action(page)
        return page.target_id

    async def next_frame(number: int) -> tuple[bytes, str, int, str]:
        check()
        return await feed.after(number)

    class Handler(BaseHTTPRequestHandler):
        timeout = FRAME_TIMEOUT_S

        def log_message(self, format: str, *args: object) -> None:
            pass

        def respond(
            self,
            status: int,
            body: bytes = b"",
            mime: str = "text/plain",
            headers: Mapping[str, str] = {},
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            with suppress(ConnectionError, TimeoutError):
                self.end_headers()
                self.wfile.write(body)

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            if self.path == "/":
                self.respond(200, html, "text/html; charset=utf-8")
            elif path == "/frame":
                self.frame(query)
            else:
                self.respond(404)

        def frame(self, query: str) -> None:
            try:
                values = parse_qs(query, strict_parsing=True, max_num_fields=1)
                number = int(values["after"][0])
                if number < 0:
                    raise ValueError
            except (KeyError, ValueError):
                self.respond(400)
                return
            if not self.authorized():
                return
            result = self.wait(next_frame(number), FRAME_WAIT_S + FRAME_TIMEOUT_S)
            if result is None:
                return
            body, target, number, size = result
            if not body:
                self.respond(204)
                return
            mime = "image/png" if body.startswith(_PNG_SIGNATURE) else "image/jpeg"
            headers = {"X-WebSkrap-Target": target, "X-WebSkrap-Frame": str(number)}
            if size:
                headers["X-WebSkrap-Size"] = size
            self.respond(200, body, mime, headers)

        def do_POST(self) -> None:
            path, _, query = self.path.partition("?")
            if allow_control and path in {"/click", "/key"}:
                if not self.authorized():
                    return
                target = self.headers.get("X-WebSkrap-Target", "")
                if not target:
                    self.respond(400)
                    return
                if path == "/click":
                    self.click(query, target)
                else:
                    self.key(target)
                return
            if not allow_scroll or path != "/scroll":
                self.respond(404)
                return
            try:
                values = parse_qs(query, strict_parsing=True, max_num_fields=4)
                x, y, dx, dy = (float(values[key][0]) for key in ("x", "y", "dx", "dy"))
                if not (0 <= x < 1 and 0 <= y < 1 and abs(dx) <= 2400 and abs(dy) <= 2400):
                    raise ValueError
            except (KeyError, ValueError):
                self.respond(400)
                return

            async def scroll(page: CDPPage) -> None:
                viewport = await cdp_session.viewport(page)
                await page.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": x * viewport["width"],
                        "y": y * viewport["height"],
                        "deltaX": dx,
                        "deltaY": dy,
                    },
                )

            self.perform(scroll)

        def click(self, query: str, target: str) -> None:
            try:
                values = parse_qs(query, strict_parsing=True, max_num_fields=2)
                x, y = (float(values[key][0]) for key in ("x", "y"))
                if not (0 <= x < 1 and 0 <= y < 1):
                    raise ValueError
            except (KeyError, ValueError):
                self.respond(400)
                return

            async def click(page: CDPPage) -> None:
                viewport = await cdp_session.viewport(page)
                position = {
                    "x": x * viewport["width"],
                    "y": y * viewport["height"],
                    "button": "left",
                    "clickCount": 1,
                }
                await page.send("Input.dispatchMouseEvent", {"type": "mousePressed", **position})
                await page.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **position})

            self.perform(click, expected_target=target)

        def key(self, target: str) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 16:
                    raise ValueError
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError
                key = body.decode("utf-8")
                if key not in _CONTROL_KEYS and not (len(key) == 1 and key.isprintable()):
                    raise ValueError
            except (ValueError, TimeoutError):
                self.respond(400)
                return

            async def press(page: CDPPage) -> None:
                if key in _CONTROL_KEYS:
                    await keyboard.press(page, key)
                else:
                    await page.send("Input.insertText", {"text": key})

            self.perform(press, expected_target=target)

        def authorized(self) -> bool:
            credential = self.headers.get("Authorization", "").encode("latin-1")
            if not secrets.compare_digest(credential, authorization.encode("ascii")):
                self.respond(404)
                return False
            if monotonic() >= deadline:
                self.respond(410)
                return False
            return True

        def perform(
            self,
            action: Callable[[CDPPage], Awaitable[None]],
            *,
            expected_target: str | None = None,
        ) -> None:
            if not self.authorized():
                return
            if self.wait(run(action, expected_target), FRAME_TIMEOUT_S) is not None:
                self.respond(204)

        def wait(self, coroutine: Coroutine[Any, Any, _T], timeout: float) -> _T | None:
            """Run ``coroutine`` on the viewer loop; on failure, respond and return None."""
            pending = asyncio.run_coroutine_threadsafe(coroutine, loop)
            try:
                return pending.result(timeout=timeout)
            except FutureTimeout:
                pending.cancel()
                self.respond(503)
            except WebSkrapError as exc:
                statuses: dict[ErrorCode | None, int] = {
                    ErrorCode.NO_SESSION: 410,
                    ErrorCode.STALE_REF: 409,
                }
                self.respond(statuses.get(exc.code, 503))
            return None

    follower = asyncio.create_task(_follow(session, directory, state["port"], feed, check))
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            ready.set_result(
                f"http://127.0.0.1:{server.server_port}/#{authorization.removeprefix('Bearer ')}"
            )
            await asyncio.sleep(LIFETIME_S)
        finally:
            follower.cancel()
            await asyncio.gather(follower, return_exceptions=True)
            await asyncio.to_thread(server.shutdown)
            thread.join()


async def start(
    session: str, *, allow_scroll: bool = False, allow_control: bool = False
) -> tuple[str, asyncio.Task[None]]:
    """Start a viewer and return its URL and lifetime task."""
    ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(
        _serve(session, ready, allow_scroll=allow_scroll, allow_control=allow_control)
    )
    try:
        await asyncio.wait((ready, task), return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            task.result()
        return ready.result(), task
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
