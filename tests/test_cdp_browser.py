"""Browser tests for the CDP driver against a local server."""

from __future__ import annotations

import asyncio
import tempfile
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from test_browser_integration import _Handler

from webskrap import ResourcePolicy, SessionConfig, WebSkrapClient
from webskrap.cdp import find_executable, launch
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.models import FetchResult

pytestmark = pytest.mark.browser

# Logs an Error whose `stack` getter records access. A driver that enabled the
# Runtime or Console domain serializes logged errors and trips it.
CONSOLE_PROBE_PAGE = b"""<html><title>Probe</title><body><p id="out"></p><script>
let touched = false;
const bait = new Error('bait');
Object.defineProperty(bait, 'stack', { get() { touched = true; return ''; } });
console.debug(bait);
setTimeout(() => {
  document.getElementById('out').textContent = JSON.stringify({
    touched, webdriver: navigator.webdriver,
  });
}, 200);
</script></body></html>"""

# Records the registration outcome in the DOM, where an isolated world reads it.
SW_PAGE = b"""<html><body><script>
navigator.serviceWorker.register('/sw.js').then(
  () => { document.body.dataset.sw = 'registered'; },
  () => { document.body.dataset.sw = 'refused'; });
</script></body></html>"""

IMAGE_PAGE = b'<html><title>Images</title><body><img src="/pixel.png">text</body></html>'

EVALUATE_CASES = [
    ("(() => 41 + 1)()", None, 42),
    ("(max) => max + 1", 2, 3),
    ("x => x * 2", 4, 8),
    ("async () => document.title", None, "CSP"),
    ("function (a) { return a.k }", {"k": "v"}, "v"),
    ("document.title;", None, "CSP"),
    ("const a = 2; a * 3", None, 6),
    ("({ a: 1 })", None, {"a": 1}),
    ("1 // note", None, 1),
    ("(() => (window.__runs = (window.__runs || 0) + 1))()", None, 1),
]


class _CDPHandler(_Handler):
    image_requests = 0

    def do_GET(self) -> None:
        pages = {"/console-probe": CONSOLE_PROBE_PAGE, "/image-page": IMAGE_PAGE, "/sw": SW_PAGE}
        if self.path == "/sw.js":
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.end_headers()
            self.wfile.write(b"self.addEventListener('fetch', () => {});")
            return
        if self.path == "/csp":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'none'")
            self.end_headers()
            self.wfile.write(b"<html><title>CSP</title><body>locked</body></html>")
            return
        if self.path in pages:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(pages[self.path])
            return
        if self.path == "/pixel.png":
            type(self).image_requests += 1
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            return
        super().do_GET()


@pytest.fixture()
def server() -> Iterator[str]:
    _CDPHandler.image_requests = 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CDPHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=3)
        httpd.server_close()


@pytest.fixture()
def config(sandbox_supported: bool) -> SessionConfig:
    return SessionConfig(channel="chromium", chromium_sandbox=sandbox_supported)


async def fetch(url: str, *, config: SessionConfig, **kwargs: Any) -> FetchResult:
    async with WebSkrapClient(default_config=config) as client:
        return await client.fetch(url, **kwargs)


async def test_fetch_reads_status_text_html_links_and_cookies(
    server: str, config: SessionConfig, tmp_path: Path
) -> None:
    page = await fetch(server, config=config, text_only=True)
    assert (page.status, page.ok, page.title, page.text) == (200, True, "Hello", "WebSkrap")
    assert page.headers["content-type"] == "text/html"

    html = await fetch(server, config=config)
    assert html.text.startswith("<html><head><title>Hello</title>")

    links = await fetch(f"{server}/links", config=config, include_links=True, max_links=2)
    assert [link.href for link in links.links] == [f"{server}/one", f"{server}/two"]
    assert links.links[0].text == "One link"
    assert links.links_total == 3

    cookie = await fetch(f"{server}/set-cookie", config=config, screenshot=tmp_path / "s.png")
    assert [(c["name"], c["value"]) for c in cookie.cookies] == [("webskrap_test", "1")]
    assert cookie.screenshot_path is not None
    assert cookie.screenshot_path.read_bytes().startswith(b"\x89PNG")


async def test_fetch_waits_for_readiness_and_reports_challenges(
    server: str, config: SessionConfig
) -> None:
    ready = await fetch(f"{server}/ready", config=config, ready_selector="#quote", text_only=True)
    assert ready.text == "Quote form ready"

    for path in ("datadome-challenge", "cf-challenge"):
        with pytest.raises(WebSkrapError) as error:
            await fetch(f"{server}/{path}", config=config)
        assert error.value.code is ErrorCode.BLOCKED

    forbidden = await fetch(f"{server}/forbidden", config=config, text_only=True)
    assert (forbidden.status, forbidden.ok, forbidden.text) == (403, False, "Forbidden")

    with pytest.raises(WebSkrapError) as invalid:
        await fetch(server, config=config, ready_selector="##")
    assert invalid.value.code is ErrorCode.USAGE


async def test_fetch_does_not_enable_runtime_or_expose_webdriver(
    server: str, config: SessionConfig
) -> None:
    result = await fetch(
        f"{server}/console-probe", config=config, ready_selector="#out:not(:empty)", text_only=True
    )
    assert result.text == '{"touched":false,"webdriver":false}'


async def test_lite_policy_blocks_images(server: str, config: SessionConfig) -> None:
    await fetch(f"{server}/image-page", config=config, wait_until="load")
    assert _CDPHandler.image_requests == 1

    lite = config.model_copy(update={"resource_policy": ResourcePolicy.LITE})
    result = await fetch(f"{server}/image-page", config=lite, wait_until="load", text_only=True)
    assert result.text == "text"
    assert _CDPHandler.image_requests == 1


async def test_navigation_errors_and_cleanup(
    config: SessionConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only WebSkrap's own temp directory moves; Chromium keeps its short TMPDIR,
    # since its singleton socket path must fit in a Unix socket address.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(WebSkrapError) as error:
        await fetch("http://webskrap-test.invalid/", config=config)
    assert error.value.code is ErrorCode.NAVIGATION
    # The temporary profile and browser log are removed with the browser.
    assert list(tmp_path.iterdir()) == []


async def test_evaluate_accepts_playwright_forms_under_a_strict_csp(
    server: str, sandbox_supported: bool
) -> None:
    browser = await launch(
        find_executable("chromium"), args=[] if sandbox_supported else ["--no-sandbox"]
    )
    try:
        page = await browser.new_page()
        await page.send("Page.enable")
        events = page.record("Page.loadEventFired")
        await page.send("Page.navigate", {"url": f"{server}/csp"})
        await events.wait(lambda *_: True, timeout_s=10, what="load")
        for script, arg, expected in EVALUATE_CASES:
            assert await page.evaluate(script, arg) == expected, script
    finally:
        await browser.close()


async def test_port_transport_attaches_over_websocket(server: str, sandbox_supported: bool) -> None:
    # Windows always launches this way; forcing it here tests that path on
    # every platform.
    browser = await launch(
        find_executable("chromium"),
        args=[] if sandbox_supported else ["--no-sandbox"],
        transport="port",
    )
    try:
        assert browser.process is not None
        page = await browser.new_page()
        response = await page.goto(server, wait_until="load", timeout_s=20)
        assert response["status"] == 200
        assert await page.evaluate("document.title") == "Hello"
    finally:
        await browser.close()
    assert browser.process.poll() is not None  # the browser process exited


@pytest.mark.parametrize(("policy", "outcome"), [("allow", "registered"), ("block", "refused")])
async def test_blocked_service_workers_cannot_register(
    server: str, config: SessionConfig, policy: str, outcome: str
) -> None:
    async with WebSkrapClient(
        default_config=config.model_copy(update={"service_workers": policy})
    ) as client:
        session = await client.session("workers")
        page = await session.new_page()
        await page.goto(f"{server}/sw", wait_until="load", timeout_s=20)
        for _ in range(50):
            if state := await page.evaluate("document.body.dataset.sw"):
                break
            await asyncio.sleep(0.1)
        assert state == outcome
