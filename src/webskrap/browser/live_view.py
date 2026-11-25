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
from webskrap.driver import cdp_session, targets
from webskrap.driver.cdp import CDPPage
from webskrap.errors import ErrorCode, WebSkrapError

LIFETIME_S = 900
FRAME_TIMEOUT_S = 5

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
<main aria-label="WebSkrap browser preview" data-scroll="false">
<img hidden alt="Live viewport of the WebSkrap browser">
<p role="status">Connecting to WebSkrap...</p>
</main>
<script>
const token = location.hash.slice(1);
const status = document.querySelector('p');
const image = document.querySelector('img');
const panel = document.querySelector('main');
const headers = {Authorization: 'Bearer ' + token};
let previous;

if (panel.dataset.scroll === 'true') {
  panel.tabIndex = 0;
  panel.setAttribute('aria-label', 'Browser preview. Scroll with the wheel or arrow keys.');
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
    if (image.hidden || event.ctrlKey || event.metaKey || event.altKey) return;
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
    image.src = next;
    if (previous) URL.revokeObjectURL(previous);
    previous = next;
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


async def _serve(session: str, ready: asyncio.Future[str], *, allow_scroll: bool) -> None:
    """Publish the viewer URL and serve it until expiry or cancellation."""
    directory, state = browser_session._open_state(session)
    loop = asyncio.get_running_loop()
    deadline = monotonic() + LIFETIME_S
    authorization = f"Bearer {secrets.token_urlsafe(32)}"

    html = _HTML.replace(b'data-scroll="false"', b'data-scroll="true"') if allow_scroll else _HTML

    async def run(action: Callable[[CDPPage], Awaitable[bytes]]) -> bytes:
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
            return await action(page)

    class Handler(BaseHTTPRequestHandler):
        timeout = FRAME_TIMEOUT_S

        def log_message(self, format: str, *args: object) -> None:
            pass

        def respond(self, status: int, body: bytes = b"", mime: str = "text/plain") -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
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

        def perform(
            self,
            action: Callable[[CDPPage], Awaitable[bytes]],
            status: int,
            mime: str = "text/plain",
        ) -> None:
            credential = self.headers.get("Authorization", "").encode("latin-1")
            if not secrets.compare_digest(credential, authorization.encode("ascii")):
                self.respond(404)
                return
            if monotonic() >= deadline:
                self.respond(410)
                return
            pending = asyncio.run_coroutine_threadsafe(run(action), loop)
            try:
                body = pending.result(timeout=FRAME_TIMEOUT_S)
            except FutureTimeout:
                pending.cancel()
                self.respond(503)
            except WebSkrapError as exc:
                self.respond(410 if exc.code is ErrorCode.NO_SESSION else 503)
            else:
                self.respond(status, body, mime)

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


async def start(session: str, *, allow_scroll: bool = False) -> tuple[str, asyncio.Task[None]]:
    """Start a viewer and return its URL and lifetime task."""
    ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(_serve(session, ready, allow_scroll=allow_scroll))
    try:
        await asyncio.wait((ready, task), return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            task.result()
        return ready.result(), task
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
