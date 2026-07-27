from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from webskrap import ResourcePolicy, SessionConfig, WebSkrapClient

pytestmark = pytest.mark.browser

# A OneTrust-shaped notice: matched by an exact CMP selector.
CMP_BANNER_PAGE = b"""<html><title>Notice</title><body>
<p>Article body</p>
<div id="onetrust-banner-sdk">
  <button id="onetrust-accept-btn-handler">Accept All Cookies</button>
  <button id="onetrust-reject-all-handler">Reject All Cookies</button>
</div>
<script>
document.getElementById('onetrust-reject-all-handler').addEventListener('click', () => {
  document.getElementById('onetrust-banner-sdk').remove();
});
</script>
</body></html>"""

# An unbranded notice injected after load: only the text strategy can find it.
LATE_TEXT_BANNER_PAGE = b"""<html><title>Notice</title><body>
<p>Article body</p>
<button id="decoy">Decline invitation</button>
<script>
setTimeout(() => {
  const bar = document.createElement('div');
  bar.className = 'cookie-consent-bar';
  bar.innerHTML = '<button>Accept</button><button>Continue without accepting</button>';
  bar.querySelectorAll('button')[1].addEventListener('click', () => bar.remove());
  document.body.appendChild(bar);
}, 400);
</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/set-cookie":
            self.send_response(200)
            self.send_header("Set-Cookie", "webskrap_test=1; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><title>Cookie</title><body>ok</body></html>")
            return

        if self.path == "/echo-cookie":
            cookie = self.headers.get("Cookie", "")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<html><title>Echo</title><body>{cookie}</body></html>".encode())
            return

        if self.path == "/cmp-banner":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(CMP_BANNER_PAGE)
            return

        if self.path == "/late-text-banner":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(LATE_TEXT_BANNER_PAGE)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><title>Hello</title><body>WebSkrap</body></html>")

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def test_server() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@pytest.mark.asyncio
async def test_fetch_local_page(test_server: str) -> None:
    try:
        async with WebSkrapClient() as client:
            result = await client.fetch(test_server)
    except Exception as exc:
        pytest.skip(f"Playwright browser unavailable: {exc}")

    assert result.status == 200
    assert result.title == "Hello"
    assert "WebSkrap" in result.text
    assert result.cookie_notice_declined is None


@pytest.mark.asyncio
async def test_declines_cmp_cookie_notice(test_server: str) -> None:
    try:
        async with WebSkrapClient() as client:
            result = await client.fetch(f"{test_server}/cmp-banner", text_only=True)
    except Exception as exc:
        pytest.skip(f"Playwright browser unavailable: {exc}")

    assert result.cookie_notice_declined == "cmp"
    assert "Reject All Cookies" not in result.text
    assert "Article body" in result.text


@pytest.mark.asyncio
async def test_declines_late_text_cookie_notice(test_server: str) -> None:
    try:
        async with WebSkrapClient() as client:
            result = await client.fetch(f"{test_server}/late-text-banner", text_only=True)
    except Exception as exc:
        pytest.skip(f"Playwright browser unavailable: {exc}")

    assert result.cookie_notice_declined == "text"
    assert "Continue without accepting" not in result.text
    # The decoy outside a consent container must never be clicked.
    assert "Decline invitation" in result.text


@pytest.mark.asyncio
async def test_decline_cookies_can_be_disabled(test_server: str) -> None:
    try:
        async with WebSkrapClient() as client:
            result = await client.fetch(
                f"{test_server}/cmp-banner",
                config=SessionConfig(decline_cookies=False),
                text_only=True,
            )
    except Exception as exc:
        pytest.skip(f"Playwright browser unavailable: {exc}")

    assert result.cookie_notice_declined is None
    assert "Reject All Cookies" in result.text


@pytest.mark.asyncio
async def test_persistent_session_reuses_cookies(test_server: str, tmp_path: Path) -> None:
    config = SessionConfig(
        user_data_dir=tmp_path / "profile",
        resource_policy=ResourcePolicy.LITE,
    )

    try:
        async with WebSkrapClient() as client:
            session = await client.session("local", config=config)
            await session.fetch(f"{test_server}/set-cookie")
            result = await session.fetch(f"{test_server}/echo-cookie")
    except Exception as exc:
        pytest.skip(f"Playwright browser unavailable: {exc}")

    assert "webskrap_test=1" in result.text
