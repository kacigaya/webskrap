"""Serve a live view of a browser session on loopback."""

from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import Awaitable, Callable
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from time import monotonic
from urllib.parse import parse_qs

from webskrap.browser import browser_session
from webskrap.driver import cdp_session, keyboard, targets
from webskrap.driver.cdp import CDPPage
from webskrap.errors import ErrorCode, WebSkrapError

LIFETIME_S = 900
FRAME_TIMEOUT_S = 5
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
img { max-width: 100vw; max-height: 100vh; }
main:focus-visible { outline: 2px solid #fafafa; outline-offset: -3px; }
p { position: fixed; bottom: 0; margin: 1rem; padding: .5rem; background: #171717; }
</style>
<main aria-label="WebSkrap browser preview" data-scroll="false" data-control="false">
<img hidden alt="Live viewport of the WebSkrap browser">
<p role="status">Connecting to WebSkrap...</p>
</main>
<script>
const token = location.hash.slice(1);
const status = document.querySelector('p');
const image = document.querySelector('img');
const panel = document.querySelector('main');
const headers = {Authorization: 'Bearer ' + token};
let previous, target;
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

async function update() {
  if (!token) {
    status.textContent = 'Open the full viewer link from WebSkrap.';
    return;
  }
  try {
    const response = await fetch('./frame', {
      headers,
      signal: AbortSignal.timeout(7000),
    });
    if (response.status === 404 || response.status === 410) {
      status.textContent = 'This viewer link has expired or is invalid.';
      status.hidden = false;
      image.hidden = true;
      return;
    }
    if (!response.ok) throw new Error();
    const next = URL.createObjectURL(await response.blob());
    const nextTarget = response.headers.get('X-WebSkrap-Target');
    if (target !== nextTarget) target = null;
    image.src = next;
    if (previous) URL.revokeObjectURL(previous);
    previous = next;
    await image.decode();
    target = nextTarget;
    image.hidden = false;
    status.hidden = true;
  } catch {
    status.textContent = 'Live view disconnected. Open a new link if this one expired.';
    status.hidden = false;
    setTimeout(update, 500);
    return;
  }
  setTimeout(update, 100);
}
update();
</script>
</html>
"""


async def _capture(page: CDPPage) -> bytes:
    shot = await page.send("Page.captureScreenshot", {"format": "jpeg", "quality": 90})
    return base64.b64decode(shot["data"])


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

    async def run(
        action: Callable[[CDPPage], Awaitable[bytes]], expected_target: str | None
    ) -> tuple[bytes, str]:
        _, current = browser_session._open_state(session)
        if (current["pid"], current["port"]) != (state["pid"], state["port"]):
            raise WebSkrapError("The watched session was replaced", code=ErrorCode.NO_SESSION)
        async with targets.attach(
            session,
            directory / "user-data",
            state["port"],
            timeout_ms=FRAME_TIMEOUT_S * 1000,
            read_only=True,
        ) as page:
            if expected_target is not None and page.target_id != expected_target:
                raise WebSkrapError("The watched tab changed", code=ErrorCode.STALE_REF)
            return await action(page), page.target_id

    class Handler(BaseHTTPRequestHandler):
        timeout = FRAME_TIMEOUT_S

        def log_message(self, format: str, *args: object) -> None:
            pass

        def respond(
            self,
            status: int,
            body: bytes = b"",
            mime: str = "text/plain",
            target: str | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            if target is not None:
                self.send_header("X-WebSkrap-Target", target)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; img-src blob:; base-uri 'none'; frame-ancestors 'none'",
            )
            with suppress(ConnectionError, TimeoutError):
                self.end_headers()
                self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                self.respond(200, html, "text/html; charset=utf-8")
            elif self.path == "/frame":
                self.perform(_capture, 200, "image/jpeg")
            else:
                self.respond(404)

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

            async def scroll(page: CDPPage) -> bytes:
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
                return b""

            self.perform(scroll, 204)

        def click(self, query: str, target: str) -> None:
            try:
                values = parse_qs(query, strict_parsing=True, max_num_fields=2)
                x, y = (float(values[key][0]) for key in ("x", "y"))
                if not (0 <= x < 1 and 0 <= y < 1):
                    raise ValueError
            except (KeyError, ValueError):
                self.respond(400)
                return

            async def click(page: CDPPage) -> bytes:
                viewport = await cdp_session.viewport(page)
                position = {
                    "x": x * viewport["width"],
                    "y": y * viewport["height"],
                    "button": "left",
                    "clickCount": 1,
                }
                await page.send("Input.dispatchMouseEvent", {"type": "mousePressed", **position})
                await page.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **position})
                return b""

            self.perform(click, 204, expected_target=target)

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

            async def press(page: CDPPage) -> bytes:
                if key in _CONTROL_KEYS:
                    await keyboard.press(page, key)
                else:
                    await page.send("Input.insertText", {"text": key})
                return b""

            self.perform(press, 204, expected_target=target)

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
            action: Callable[[CDPPage], Awaitable[bytes]],
            status: int,
            mime: str = "text/plain",
            *,
            expected_target: str | None = None,
        ) -> None:
            if not self.authorized():
                return
            pending = asyncio.run_coroutine_threadsafe(run(action, expected_target), loop)
            try:
                body, target = pending.result(timeout=FRAME_TIMEOUT_S)
            except FutureTimeout:
                pending.cancel()
                self.respond(503)
            except WebSkrapError as exc:
                statuses: dict[ErrorCode | None, int] = {
                    ErrorCode.NO_SESSION: 410,
                    ErrorCode.STALE_REF: 409,
                }
                self.respond(statuses.get(exc.code, 503))
            else:
                self.respond(status, body, mime, target if status == 200 else None)

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            ready.set_result(
                f"http://127.0.0.1:{server.server_port}/#{authorization.removeprefix('Bearer ')}"
            )
            await asyncio.sleep(LIFETIME_S)
        finally:
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
