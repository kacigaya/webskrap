"""Live reCAPTCHA v2 and hCaptcha tests against the providers' official test keys.

reCAPTCHA's test site key always shows the checkbox and never asks for images
(https://developers.google.com/recaptcha/docs/faq). hCaptcha's test site key
never asks a question and returns the fixed token
``10000000-aaaa-bbbb-cccc-000000000001`` (https://docs.hcaptcha.com/#integration-testing-test-keys).
hCaptcha refuses ``localhost`` and ``127.0.0.1`` as hostnames, so the local
server is reached as ``captcha-test.example`` through a resolver rule.

The blocking page answers 403 and reloads into the real page once the widget
calls back. The form page answers 200 and only records the token. Both need
network access to the providers, so they are opt-in:

    WEBSKRAP_LIVE=1 pytest tests/live/test_recaptcha_live.py
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from test_turnstile import _modes

from webskrap import SessionConfig, WebSkrapClient, browser_session

pytestmark = [pytest.mark.browser, pytest.mark.live]

HOST = "captcha-test.example"
WIDGETS = {
    "recaptcha": (
        "https://www.google.com/recaptcha/api.js",
        "g-recaptcha",
        "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI",
    ),
    "hcaptcha": (
        "https://js.hcaptcha.com/1/api.js",
        "h-captcha",
        "10000000-ffff-ffff-ffff-000000000001",
    ),
}
HCAPTCHA_TOKEN = "10000000-aaaa-bbbb-cccc-000000000001"


def _page(widget: str, reload: bool) -> bytes:
    script, css_class, sitekey = WIDGETS[widget]
    then = "document.cookie = 'cleared=1; path=/'; location.reload();" if reload else ""
    return f"""<html><head><title>Verify</title><script src="{script}" async defer></script>
</head><body><form><div class="{css_class}" data-sitekey="{sitekey}" data-callback="passed">
</div></form><script>function passed(token) {{
  fetch('/token?value=' + encodeURIComponent(token)).then(() => {{ {then} }});
}}</script></body></html>""".encode()


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        kind, _, widget = url.path.strip("/").partition("/")
        if widget not in WIDGETS and url.path != "/token":
            self._send(404, b"")
        elif url.path == "/token":
            self.server.tokens += parse_qs(url.query)["value"]  # type: ignore[attr-defined]
            self._send(204, b"")
        elif kind == "form":
            self._send(200, _page(widget, reload=False))
        elif "cleared=1" in self.headers.get("Cookie", ""):
            self._send(200, b"<html><title>Real page</title><body>Welcome back</body></html>")
        else:
            self._send(403, _page(widget, reload=True))

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture(autouse=True)
def _require_live() -> None:
    if not os.environ.get("WEBSKRAP_LIVE"):
        pytest.skip("set WEBSKRAP_LIVE=1 to run live reCAPTCHA and hCaptcha tests")


@pytest.fixture
def widget_server() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.tokens = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _config(sandbox_supported: bool, mode: dict[str, Any]) -> SessionConfig:
    return SessionConfig(
        channel=os.environ.get("WEBSKRAP_BROWSER_CHANNEL", "chromium"),
        chromium_sandbox=sandbox_supported,
        navigation_timeout_ms=60_000,
        solve_recaptcha=True,
        launch_args=[f"--host-resolver-rules=MAP {HOST} 127.0.0.1"],
        **mode,
    )


def _check_token(widget: str, tokens: list[str]) -> None:
    [token] = tokens
    assert token == HCAPTCHA_TOKEN if widget == "hcaptcha" else len(token) > 100


@pytest.mark.parametrize("widget", list(WIDGETS))
@pytest.mark.parametrize("mode", _modes())
async def test_fetch_clicks_the_checkbox_on_a_blocking_page(
    widget_server, sandbox_supported, mode: dict[str, Any], widget: str
) -> None:
    url = f"http://{HOST}:{widget_server.server_port}/gate/{widget}"
    async with WebSkrapClient(default_config=_config(sandbox_supported, mode)) as client:
        result = await client.fetch(url, text_only=True)
    assert (result.status, result.title, result.text) == (200, "Real page", "Welcome back")
    _check_token(widget, widget_server.tokens)


@pytest.mark.parametrize("widget", list(WIDGETS))
async def test_solve_captcha_ticks_a_form_checkbox(
    widget_server, sandbox_supported, widget: str
) -> None:
    url = f"http://{HOST}:{widget_server.server_port}/form/{widget}"
    async with WebSkrapClient(default_config=_config(sandbox_supported, {})) as client:
        page = await (await client.session("form")).new_page()
        await page.goto(url, wait_until="load", timeout_s=60)
        state = await browser_session.solve_captcha(page)
    provider = "hCaptcha" if widget == "hcaptcha" else "reCAPTCHA"
    assert state["captcha"] == {"provider": provider, "state": "checked"}
    _check_token(widget, widget_server.tokens)
