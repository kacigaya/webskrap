"""Session setup reaches cross-site iframes and workers, not only the page."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import pytest

from webskrap import ProxyConfig, ResourcePolicy, SessionConfig, WebSkrapClient, cdp_dom
from webskrap.models import BrowserProfile

pytestmark = pytest.mark.browser

TOKEN = "Basic " + base64.b64encode(b"user:secret").decode()
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
# Each document starts a worker that loads a font and reports what it sees;
# the document writes the report into its own DOM, where an isolated world
# reads it.
IDENTITY = (
    "({ua: navigator.userAgent, tz: Intl.DateTimeFormat().resolvedOptions().timeZone,"
    " langs: navigator.languages.join(',')})"
)
DOCUMENT = """<img src="/{name}.png"><script>
const worker = new Worker('/{name}-worker.js');
worker.onmessage = (e) => {{ document.body.dataset.worker = JSON.stringify(e.data); }};
</script>{extra}"""
WORKER = (
    "new FontFace('f', 'url(/NAME.woff)').load().catch(() => {}); postMessage(" + IDENTITY + ");"
)
PAGES = {
    "/": DOCUMENT.format(
        name="top",
        extra='<iframe src="http://site-b.test/frame"></iframe>'
        "<script>new SharedWorker('/shared.js');</script>",
    ),
    "/frame": DOCUMENT.format(name="frame", extra=""),
    "/top-worker.js": WORKER.replace("NAME", "top-worker"),
    "/frame-worker.js": WORKER.replace("NAME", "frame-worker"),
    "/shared.js": "new FontFace('f', 'url(/shared.woff)').load().catch(() => {});",
}


class _Proxy(BaseHTTPRequestHandler):
    """A forward proxy that is also the origin of every site it is asked for.

    ``site-a.test`` and ``site-b.test`` are different sites, so the iframe
    runs out of process. With ``require_auth`` it answers 407 to requests
    without the test credentials.
    """

    require_auth = False
    requests: list[dict[str, str]] = []

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        authed = self.headers.get("Proxy-Authorization") == TOKEN
        self.requests.append(
            {
                "url": f"{url.hostname}{url.path}",
                "ua": self.headers.get("User-Agent", ""),
                "authed": str(authed),
            }
        )
        if self.require_auth and not authed:
            self.send_response(407)
            self.send_header("Proxy-Authenticate", 'Basic realm="test"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = PNG if url.path.endswith(".png") else PAGES.get(url.path, "").encode()
        kind = "text/javascript" if url.path.endswith(".js") else "text/html"
        self.send_response(200)
        self.send_header("Content-Type", "image/png" if url.path.endswith(".png") else kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture()
def proxy() -> Iterator[str]:
    _Proxy.requests = []
    _Proxy.require_auth = False
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Proxy)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=3)
        httpd.server_close()


async def _visit(config: SessionConfig, profile: BrowserProfile | None = None) -> dict[str, Any]:
    """Load the test site; return each frame's worker report, by frame URL."""
    async with WebSkrapClient(default_config=config) as client:
        session = await client.session("children", profile=profile)
        page = await session.new_page()
        await page.goto("http://site-a.test/", wait_until="load", timeout_s=20)
        reports: dict[str, Any] = {}
        for _ in range(50):
            frames = await cdp_dom.frames(page)
            for frame in frames:
                identity = await frame.page.evaluate(f"() => ({IDENTITY})")
                worker = await frame.page.evaluate("document.body?.dataset.worker")
                reports[frame.url] = {"document": identity, "worker": worker}
            if len(frames) == 2 and all(report["worker"] for report in reports.values()):
                break
            await asyncio.sleep(0.1)
        # Give the shared worker's font a moment to be requested.
        await asyncio.sleep(0.5)
        return reports


@pytest.mark.parametrize("credentials", [False, True])
async def test_lite_policy_covers_cross_site_frames_and_workers(
    proxy: str, sandbox_supported: bool, credentials: bool
) -> None:
    _Proxy.require_auth = credentials
    login = {"username": "user", "password": "secret"} if credentials else {}
    config = SessionConfig(
        chromium_sandbox=sandbox_supported,
        decline_cookies=False,
        resource_policy=ResourcePolicy.LITE,
        proxy=ProxyConfig(server=proxy, **login),
    )
    reports = await _visit(config)

    assert set(reports) == {"http://site-a.test/", "http://site-b.test/frame"}
    loaded = {request["url"] for request in _Proxy.requests}
    assert {"site-b.test/frame", "site-b.test/frame-worker.js", "site-a.test/shared.js"} <= loaded
    # Images and fonts stay blocked in the cross-site frame and every worker.
    assert not [url for url in loaded if url.endswith((".png", ".woff"))]
    if credentials:
        site = [r for r in _Proxy.requests if r["url"].startswith(("site-a", "site-b"))]
        assert site[0]["authed"] == "False"  # the first one meets the challenge
        assert all(request["authed"] == "True" for request in site[1:])


async def test_full_profile_reaches_cross_site_frames_and_workers(
    proxy: str, sandbox_supported: bool
) -> None:
    profile = BrowserProfile(
        name="child-targets",
        user_agent="Mozilla/5.0 (X11; Linux x86_64) WebSkrapTest/1.0",
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
        navigator_languages=["ja-JP", "ja"],
    )
    config = SessionConfig(
        chromium_sandbox=sandbox_supported,
        decline_cookies=False,
        profile_mode="full",
        proxy=ProxyConfig(server=proxy),
    )
    reports = await _visit(config, profile)

    expected = {"ua": profile.user_agent, "tz": "Asia/Tokyo"}
    for url, report in reports.items():
        worker = json.loads(report["worker"])
        for seen in (report["document"], worker):
            assert {key: seen[key] for key in expected} == expected, url
            assert seen["langs"].startswith("ja-JP,ja"), url
    frame_requests = [r for r in _Proxy.requests if r["url"].startswith("site-b.test")]
    assert frame_requests
    assert all(request["ua"] == profile.user_agent for request in frame_requests)


async def test_native_profile_leaves_child_targets_alone(sandbox_supported: bool) -> None:
    # Nothing to apply, so pages are not auto-attached and frames start at once.
    async with WebSkrapClient(
        default_config=SessionConfig(chromium_sandbox=sandbox_supported, decline_cookies=False)
    ) as client:
        session = await client.session("native")
        page = await session.new_page()
        assert page.iframe_targets is None
