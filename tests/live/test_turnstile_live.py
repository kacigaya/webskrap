"""Live Turnstile test against Cloudflare's official testing sitekey.

``3x00000000000000000000FF`` always forces an interactive checkbox and works
on any hostname, including localhost; a pass returns the dummy token
``XXXX.DUMMY.TOKEN.XXXX``. See
https://developers.cloudflare.com/turnstile/troubleshooting/testing/.

The local page stands in for Cloudflare's "Just a moment" interstitial: it
answers 403 with ``cf-mitigated: challenge`` and reloads into the real page
once the widget passes. It needs network access to challenges.cloudflare.com,
so it is opt-in:

    WEBSKRAP_LIVE=1 pytest tests/live/test_turnstile_live.py
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import browser_modes

from webskrap import SessionConfig, WebSkrapClient

pytestmark = [pytest.mark.browser, pytest.mark.live]

FORCE_INTERACTIVE_SITEKEY = "3x00000000000000000000FF"
DUMMY_TOKEN = "XXXX.DUMMY.TOKEN.XXXX"

INTERSTITIAL = f"""<html><head><title>Just a moment...</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
</head><body><div class="cf-turnstile" data-sitekey="{FORCE_INTERACTIVE_SITEKEY}"
  data-callback="passed"></div>
<script>function passed(token) {{
  fetch('/token?value=' + encodeURIComponent(token)).then(() => {{
    document.cookie = 'cleared=1; path=/';
    location.reload();
  }});
}}</script></body></html>""".encode()


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, **headers: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/token":
            self.server.tokens += parse_qs(url.query)["value"]  # type: ignore[attr-defined]
            self._send(204, b"")
        elif "cleared=1" in self.headers.get("Cookie", ""):
            self._send(200, b"<html><title>Real page</title><body>Welcome back</body></html>")
        else:
            self._send(403, INTERSTITIAL, cf_mitigated="challenge")

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture(autouse=True)
def _require_live() -> None:
    if not os.environ.get("WEBSKRAP_LIVE"):
        pytest.skip("set WEBSKRAP_LIVE=1 to run live Turnstile tests")


@pytest.fixture
def interstitial_server() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.tokens = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("mode", browser_modes())
async def test_fetch_passes_the_forced_interactive_test_sitekey(
    interstitial_server, sandbox_supported, mode: dict[str, Any]
) -> None:
    config = SessionConfig(
        channel=os.environ.get("WEBSKRAP_BROWSER_CHANNEL", "chromium"),
        chromium_sandbox=sandbox_supported,
        navigation_timeout_ms=60_000,
        solve_cloudflare=True,
        **mode,
    )
    url = f"http://127.0.0.1:{interstitial_server.server_port}/"
    async with WebSkrapClient(default_config=config) as client:
        result = await client.fetch(url, text_only=True)
    assert (result.status, result.title, result.text) == (200, "Real page", "Welcome back")
    assert interstitial_server.tokens == [DUMMY_TOKEN]
