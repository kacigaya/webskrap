from __future__ import annotations

import asyncio
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from webskrap import ResourcePolicy, SessionConfig, WebSkrapClient, browser_session
from webskrap.client import lavapipe_available

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

# Relative, duplicate, and javascript: anchors: link collection must resolve,
# deduplicate, and drop them respectively.
LINKS_PAGE = b"""<html><title>Links</title><body>
<a href="/one">  One\n  link  </a>
<a href="/two">Two</a>
<a href="/one">One again</a>
<a href="javascript:void(0)">Script</a>
<a href="https://example.test/three">Three</a>
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

        if self.path == "/links":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(LINKS_PAGE)
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
async def test_fetch_local_page(test_server: str, sandbox_supported: bool) -> None:
    async with WebSkrapClient(
        default_config=SessionConfig(chromium_sandbox=sandbox_supported)
    ) as client:
        result = await client.fetch(test_server)

    assert result.status == 200
    assert result.title == "Hello"
    assert "WebSkrap" in result.text
    assert result.cookie_notice_declined is None


@pytest.mark.asyncio
async def test_declines_cmp_cookie_notice(test_server: str, sandbox_supported: bool) -> None:
    config = SessionConfig(chromium_sandbox=sandbox_supported, decline_cookies=True)
    async with WebSkrapClient(
        default_config=SessionConfig(chromium_sandbox=sandbox_supported)
    ) as client:
        result = await client.fetch(f"{test_server}/cmp-banner", config=config, text_only=True)

    assert result.cookie_notice_declined == "cmp"
    assert "Reject All Cookies" not in result.text
    assert "Article body" in result.text


@pytest.mark.asyncio
async def test_declines_late_text_cookie_notice(test_server: str, sandbox_supported: bool) -> None:
    config = SessionConfig(chromium_sandbox=sandbox_supported, decline_cookies=True)
    async with WebSkrapClient(
        default_config=SessionConfig(chromium_sandbox=sandbox_supported)
    ) as client:
        result = await client.fetch(
            f"{test_server}/late-text-banner",
            config=config,
            text_only=True,
        )

    assert result.cookie_notice_declined == "text"
    assert "Continue without accepting" not in result.text
    # The decoy outside a consent container must never be clicked.
    assert "Decline invitation" in result.text


@pytest.mark.asyncio
async def test_decline_cookies_can_be_disabled(test_server: str, sandbox_supported: bool) -> None:
    async with WebSkrapClient(
        default_config=SessionConfig(chromium_sandbox=sandbox_supported)
    ) as client:
        result = await client.fetch(
            f"{test_server}/cmp-banner",
            config=SessionConfig(chromium_sandbox=sandbox_supported, decline_cookies=False),
            text_only=True,
        )

    assert result.cookie_notice_declined is None
    assert "Reject All Cookies" in result.text


@pytest.mark.asyncio
async def test_persistent_session_reuses_cookies(
    test_server: str, tmp_path: Path, sandbox_supported: bool
) -> None:
    config = SessionConfig(
        chromium_sandbox=sandbox_supported,
        user_data_dir=tmp_path / "profile",
        resource_policy=ResourcePolicy.LITE,
    )

    async with WebSkrapClient(
        default_config=SessionConfig(chromium_sandbox=sandbox_supported)
    ) as client:
        session = await client.session("local", config=config)
        await session.fetch(f"{test_server}/set-cookie")
        result = await session.fetch(f"{test_server}/echo-cookie")

    assert "webskrap_test=1" in result.text


@pytest.mark.asyncio
async def test_per_call_patchright_config_starts_patchright(sandbox_supported: bool) -> None:
    config = SessionConfig(
        chromium_sandbox=sandbox_supported, driver="patchright", channel=None, decline_cookies=False
    )

    async with WebSkrapClient(
        default_config=SessionConfig(chromium_sandbox=sandbox_supported)
    ) as client:
        session = await client.session("patchright", config=config)

    assert type(session.context).__module__.startswith("patchright.")


@pytest.mark.asyncio
async def test_links_are_off_by_default(test_server: str, sandbox_supported: bool) -> None:
    async with WebSkrapClient(
        default_config=SessionConfig(chromium_sandbox=sandbox_supported)
    ) as client:
        result = await client.fetch(f"{test_server}/links")

    assert result.links == []
    assert result.links_total == 0


@pytest.mark.asyncio
async def test_links_are_resolved_deduplicated_and_normalized(
    test_server: str, sandbox_supported: bool
) -> None:
    async with WebSkrapClient(
        default_config=SessionConfig(chromium_sandbox=sandbox_supported)
    ) as client:
        result = await client.fetch(f"{test_server}/links", include_links=True)

    assert result.links_total == 3
    assert [link.href for link in result.links] == [
        f"{test_server}/one",
        f"{test_server}/two",
        "https://example.test/three",
    ]
    # Whitespace inside the anchor collapses, so the label stays one short line.
    assert result.links[0].text == "One link"


@pytest.mark.asyncio
async def test_links_are_capped_but_still_counted(
    test_server: str, sandbox_supported: bool
) -> None:
    async with WebSkrapClient(
        default_config=SessionConfig(chromium_sandbox=sandbox_supported)
    ) as client:
        result = await client.fetch(f"{test_server}/links", include_links=True, max_links=2)

    assert len(result.links) == 2
    assert result.links_total == 3


@pytest.mark.asyncio
async def test_links_are_skipped_when_javascript_is_disabled(
    test_server: str, sandbox_supported: bool
) -> None:
    config = SessionConfig(chromium_sandbox=sandbox_supported, java_script_enabled=False)

    async with WebSkrapClient(
        default_config=SessionConfig(chromium_sandbox=sandbox_supported)
    ) as client:
        result = await client.fetch(f"{test_server}/links", config=config, include_links=True)

    assert result.links == []
    assert result.links_total == 0


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("Xvfb") is None,
    reason="needs Linux with Xvfb",
)
async def test_virtual_display_presents_a_headed_browser(
    test_server: str, sandbox_supported: bool
) -> None:
    # Headed on Xvfb: no HeadlessChrome token anywhere and full client hints,
    # which a --user-agent rewrite of headless Chrome cannot give.
    config = SessionConfig(
        chromium_sandbox=sandbox_supported,
        driver="patchright",
        channel="chromium",
        virtual_display=True,
    )
    async with WebSkrapClient(default_config=config) as client:
        session = await client.session("virtual-display", config=config)
        page = await session.context.new_page()
        # userAgentData needs a secure context; a data: URL is not one.
        await page.goto(f"{test_server}/links")
        seen = await page.evaluate(
            """async () => ({
                ua: navigator.userAgent,
                brands: navigator.userAgentData.brands.map((b) => b.brand),
                hints: await navigator.userAgentData.getHighEntropyValues(
                    ['architecture', 'bitness', 'uaFullVersion']),
                screen: [screen.width, screen.height],
            })"""
        )

    assert "HeadlessChrome" not in seen["ua"]
    assert "HeadlessChrome" not in seen["brands"]
    assert seen["hints"]["architecture"]
    assert seen["hints"]["bitness"]
    assert seen["hints"]["uaFullVersion"]
    assert seen["screen"] == [1920, 1080]


@pytest.mark.skipif(not lavapipe_available(), reason="needs Mesa's lavapipe Vulkan driver")
async def test_mesa_gpu_replaces_swiftshader(test_server: str, sandbox_supported: bool) -> None:
    config = SessionConfig(
        chromium_sandbox=sandbox_supported, driver="patchright", channel="chromium", gpu="mesa"
    )
    async with WebSkrapClient(default_config=config) as client:
        session = await client.session("mesa-gpu", config=config)
        page = await session.context.new_page()
        await page.goto(f"{test_server}/links")
        renderer = await page.evaluate(
            """() => {
                const gl = document.createElement('canvas').getContext('webgl');
                const info = gl && gl.getExtension('WEBGL_debug_renderer_info');
                return info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : null;
            }"""
        )

    assert renderer is not None
    assert "llvmpipe" in renderer
    assert "SwiftShader" not in renderer


_ICE_CANDIDATE_TYPES = """async () => {
    const pc = new RTCPeerConnection();
    const types = [];
    pc.onicecandidate = (e) => { if (e.candidate) types.push(e.candidate.type); };
    pc.createDataChannel('probe');
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise((resolve) => setTimeout(resolve, 1500));
    pc.close();
    return types;
}"""


@pytest.mark.parametrize(
    ("policy", "expect_candidates"),
    [(None, True), ("disable_non_proxied_udp", False)],
)
def test_persistent_session_applies_the_webrtc_policy(
    persistent_session_env: Path, policy: str | None, expect_candidates: bool
) -> None:
    # No ICE servers: host candidates are gathered locally, so this needs no
    # network. The policy has to reach the detached browser's command line.
    name = "webrtc-policy"

    async def exercise() -> list[str]:
        await browser_session.open_session(name, webrtc_ip_handling_policy=policy)
        return await browser_session.run_page_action(
            name, lambda page: browser_session.evaluate(page, _ICE_CANDIDATE_TYPES)
        )

    try:
        types = asyncio.run(exercise())
    finally:
        browser_session.close_session(name, delete_data=True)

    assert bool(types) is expect_candidates
