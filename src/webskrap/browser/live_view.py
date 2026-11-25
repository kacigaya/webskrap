"""Serve a read-only view of a browser session on loopback."""

from __future__ import annotations

import asyncio
import base64
import secrets
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from time import monotonic

from webskrap.browser import browser_session
from webskrap.driver import targets
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
img:not([hidden]) { display: block; margin: auto; max-width: 100%; max-height: 100vh; }
p { margin: 1rem; }
</style>
<p role="status">Connecting to WebSkrap...</p>
<img hidden alt="Live viewport of the WebSkrap browser">
<script>
const token = location.hash.slice(1);
const status = document.querySelector('p');
const image = document.querySelector('img');
let previous;
async function update() {
  if (!token) {
    status.textContent = 'Open the full viewer link from WebSkrap.';
    return;
  }
  try {
    const response = await fetch('./frame', {
      headers: {Authorization: 'Bearer ' + token},
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
  }
  setTimeout(update, 500);
}
update();
</script>
</html>
"""


async def _capture(page: CDPPage) -> bytes:
    shot = await page.send("Page.captureScreenshot", {"format": "jpeg", "quality": 65})
    return base64.b64decode(shot["data"])


async def _serve(session: str, ready: asyncio.Future[str]) -> None:
    """Publish the viewer URL and serve it until expiry or cancellation."""
    directory, state = browser_session._open_state(session)
    loop = asyncio.get_running_loop()
    deadline = monotonic() + LIFETIME_S
    authorization = f"Bearer {secrets.token_urlsafe(32)}"

    async def frame() -> bytes:
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
            return await _capture(page)

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
                self.respond(200, _HTML, "text/html; charset=utf-8")
                return
            credential = self.headers.get("Authorization", "").encode("latin-1")
            if self.path != "/frame" or not secrets.compare_digest(
                credential, authorization.encode("ascii")
            ):
                self.respond(404)
                return
            if monotonic() >= deadline:
                self.respond(410)
                return
            pending = asyncio.run_coroutine_threadsafe(frame(), loop)
            try:
                image = pending.result(timeout=FRAME_TIMEOUT_S)
            except FutureTimeout:
                pending.cancel()
                self.respond(503)
            except WebSkrapError as exc:
                self.respond(410 if exc.code is ErrorCode.NO_SESSION else 503)
            else:
                self.respond(200, image, "image/jpeg")

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


async def start(session: str) -> tuple[str, asyncio.Task[None]]:
    """Start a viewer and return its URL and lifetime task."""
    ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(_serve(session, ready))
    try:
        await asyncio.wait((ready, task), return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            task.result()
        return ready.result(), task
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
