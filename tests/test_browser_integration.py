from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from webskrap import ResourcePolicy, SessionConfig, WebSkrapClient, browser_session, mcp_server
from webskrap.client import lavapipe_available
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.fetch_runtime import FetchRuntime

pytestmark = pytest.mark.browser


async def test_mcp_protocol_reuses_fetch_sessions_and_keeps_unnamed_calls_isolated(
    test_server: str, sandbox_supported: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBSKRAP_ALLOW_PRIVATE_NET", "1")
    monkeypatch.setenv("WEBSKRAP_CHROMIUM_SANDBOX", "1" if sandbox_supported else "0")
    runtimes: list[FetchRuntime] = []

    class CapturingFetchRuntime(FetchRuntime):
        def __init__(self):
            super().__init__()
            runtimes.append(self)

    monkeypatch.setattr(mcp_server, "FetchRuntime", CapturingFetchRuntime)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        server = asyncio.create_task(
            mcp_server.mcp._lowlevel_server.run(
                *server_streams, mcp_server.mcp._lowlevel_server.create_initialization_options()
            )
        )
        try:
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                common = {
                    "channel": "chromium",
                    "decline_cookies": False,
                    "wait_until": "load",
                    "timeout_ms": 60_000,
                    "ready_selector": "body",
                }
                result = await client.call_tool(
                    "fetch", {"url": f"{test_server}/set-cookie", "session": "crawl", **common}
                )
                assert not result.is_error
                result = await client.call_tool(
                    "stealth_fetch",
                    {"url": f"{test_server}/echo-cookie", "session": "crawl", **common},
                )
                assert not result.is_error
                assert "webskrap_test=1" in result.structured_content["text"]
                results = await asyncio.gather(
                    *(
                        client.call_tool(
                            "stealth_fetch",
                            {"url": test_server, "session": "crawl", **common},
                        )
                        for _ in range(3)
                    )
                )
                assert all(not result.is_error for result in results)
                assert all(result.structured_content["text"] == "WebSkrap" for result in results)
                result = await client.call_tool(
                    "fetch", {"url": f"{test_server}/echo-cookie", **common}
                )
                assert not result.is_error
                assert "webskrap_test" not in result.structured_content["text"]
                result = await client.call_tool(
                    "stealth_fetch", {"url": f"{test_server}/cf-challenge", **common}
                )
                assert result.is_error
                assert "[code: blocked]" in result.content[0].text
                result = await client.call_tool(
                    "fetch",
                    {
                        "url": test_server,
                        "channel": "chromium",
                        "decline_cookies": False,
                        "timeout_ms": 250,
                        "ready_selector": "#missing",
                    },
                )
                assert result.is_error
                assert "[code: timeout]" in result.content[0].text
                result = await client.call_tool("fetch_session_close", {"session": "crawl"})
                assert not result.is_error
                assert result.structured_content == {"closed": True}
        finally:
            server.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server
    assert runtimes[0].client._sessions == {}


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


# Clears itself as Akamai's sec-cpt page does: its script sets a cookie and reloads.
INTERSTITIAL_PAGE = b"""<html><body><div id="sec-if-cpt-container" style="display:none"></div>
<script>setTimeout(() => { document.cookie = 'passed=1; path=/'; location.reload(); }, 300);
</script></body></html>"""

# Reloads before DOMContentLoaded: the slow script holds the parser until the
# reload has replaced the document.
EARLY_RELOAD_PAGE = b"""<html><head><script>
document.cookie = 'early=1; path=/'; location.reload();
</script><script src="/slow.js"></script></head><body>First</body></html>"""

BLOCKS = Path(__file__).parent / "fixtures" / "blocks"


class _Handler(BaseHTTPRequestHandler):
    server_name = "BaseHTTP"

    def version_string(self) -> str:
        return self.server_name

    def _passed_page(self, cookie: str, title: bytes) -> bool:
        if cookie not in self.headers.get("Cookie", ""):
            return False
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><title>" + title + b"</title><body>Real page</body></html>")
        return True

    def do_GET(self) -> None:
        if self.path == "/interstitial":
            if not self._passed_page("passed=1", b"Passed"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(INTERSTITIAL_PAGE)
            return

        if self.path == "/waf-challenge":
            if not self._passed_page("passed=1", b"Passed"):
                self.send_response(202)
                self.send_header("Content-Type", "text/html")
                self.send_header("x-amzn-waf-action", "challenge")
                self.end_headers()
                self.wfile.write(INTERSTITIAL_PAGE.replace(b"sec-if-cpt-container", b"x"))
            return

        if self.path == "/early-reload":
            if not self._passed_page("early=1", b"Reloaded"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(EARLY_RELOAD_PAGE)
            return

        if self.path == "/slow.js":
            time.sleep(3)
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.end_headers()
            return

        if self.path == "/cf-block":
            self.server_name = "cloudflare"
            self.send_response(403)
            self.send_header("Content-Type", "text/html")
            self.send_header("cf-ray", "0123456789abcdef-CDG")
            self.end_headers()
            self.wfile.write((BLOCKS / "cloudflare-custom-block.html").read_bytes())
            return

        if self.path == "/cf-challenge":
            self.send_response(200)
            self.send_header("cf-mitigated", "challenge")
            self.end_headers()
            self.wfile.write(b"<html><body>Verify your browser</body></html>")
            return

        if self.path == "/datadome-challenge":
            self.send_response(403)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b'<html><body><script src="https://ct.captcha-delivery.com/c.js"></script>'
                b"</body></html>"
            )
            return

        if self.path == "/forbidden":
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"<html><body>Forbidden</body></html>")
            return

        if self.path == "/ready":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b'<html><body><div id="quote" hidden>Quote form ready</div>'
                b'<script>addEventListener("load", async () => {'
                b'await fetch("/ready-data"); document.getElementById("quote").hidden = false;'
                b"});</script></body></html>"
            )
            return

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


async def _page_targets(session, expected: set[str] | None = None) -> set[str]:
    # Target.closeTarget returns before the target is gone; give it a moment.
    for _ in range(20):
        targets = await session.browser.connection.send("Target.getTargets")
        found = {info["targetId"] for info in targets["targetInfos"] if info["type"] == "page"}
        if expected is None or found == expected:
            break
        await asyncio.sleep(0.1)
    return found


async def test_fetch_challenge_readiness_and_http_errors(test_server, sandbox_supported):
    config = SessionConfig(
        channel="chromium",
        chromium_sandbox=sandbox_supported,
        # The challenge pages load DataDome's script; no request may leave the test.
        launch_args=["--host-resolver-rules=MAP *.captcha-delivery.com ~NOTFOUND"],
    )
    async with WebSkrapClient(default_config=config) as client:
        session = await client.session("quote")
        original_pages = await _page_targets(session)
        result = await session.fetch(
            f"{test_server}/ready", ready_selector="#quote", text_only=True
        )
        assert result.text == "Quote form ready"
        for path in ("datadome-challenge", "cf-challenge"):
            with pytest.raises(WebSkrapError) as error:
                await session.fetch(f"{test_server}/{path}", ready_selector="#quote")
            assert error.value.code is ErrorCode.BLOCKED
            assert await _page_targets(session, original_pages) == original_pages
        result = await session.fetch(f"{test_server}/forbidden", text_only=True)
        assert result.status == 403
        assert not result.ok
        assert result.text == "Forbidden"


async def test_fetch_waits_out_self_clearing_interstitials(test_server, sandbox_supported):
    config = SessionConfig(channel="chromium", chromium_sandbox=sandbox_supported)
    async with WebSkrapClient(default_config=config) as client:
        for path in ("interstitial", "waf-challenge"):
            session = await client.session(path)
            result = await session.fetch(f"{test_server}/{path}", text_only=True)
            # The reloaded document's response, not the interstitial's 202.
            assert (result.status, result.title, result.text) == (200, "Passed", "Real page")


async def test_fetch_follows_a_reload_before_domcontentloaded(test_server, sandbox_supported):
    config = SessionConfig(channel="chromium", chromium_sandbox=sandbox_supported)
    async with WebSkrapClient(default_config=config) as client:
        session = await client.session("early")
        result = await session.fetch(f"{test_server}/early-reload", timeout_ms=10_000)
    assert (result.status, result.title) == (200, "Reloaded")


async def test_fetch_reports_a_provider_block_page(test_server, sandbox_supported):
    config = SessionConfig(channel="chromium", chromium_sandbox=sandbox_supported)
    async with WebSkrapClient(default_config=config) as client:
        session = await client.session("block")
        with pytest.raises(WebSkrapError) as error:
            await session.fetch(f"{test_server}/cf-block")
    assert error.value.code is ErrorCode.BLOCKED
    assert str(error.value) == (
        "Cloudflare blocked this client instead of serving the requested page"
    )


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
        channel="chromium",
        virtual_display=True,
    )
    async with WebSkrapClient(default_config=config) as client:
        session = await client.session("virtual-display", config=config)
        page = await session.new_page()
        # userAgentData needs a secure context; a data: URL is not one.
        await page.goto(f"{test_server}/links", wait_until="load", timeout_s=30)
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
    config = SessionConfig(chromium_sandbox=sandbox_supported, channel="chromium", gpu="mesa")
    async with WebSkrapClient(default_config=config) as client:
        session = await client.session("mesa-gpu", config=config)
        page = await session.new_page()
        await page.goto(f"{test_server}/links", wait_until="load", timeout_s=30)
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
    const pc = new RTCPeerConnection({iceServers: []});
    const types = [];
    let timeout;
    try {
        const complete = new Promise((resolve, reject) => {
            timeout = setTimeout(() => reject(new Error('ICE gathering timed out')), 10000);
            pc.onicecandidate = (e) => {
                if (e.candidate) types.push(e.candidate.type);
                else resolve();
            };
        });
        pc.createDataChannel('probe');
        await pc.setLocalDescription(await pc.createOffer());
        await complete;
        return types;
    } finally {
        clearTimeout(timeout);
        pc.close();
    }
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


# A button below the fold and a text field, with every input event the
# page's own scripts would see recorded on window.__events.
_BEHAVIOR_PAGE = (
    "data:text/html,<title>behavior</title>"
    "<input id=q style='position:absolute;top:2600px'>"
    "<button id=go style='position:absolute;top:2400px;left:400px'>Go</button>"
    "<div style='height:4000px'></div>"
    "<script>window.__events=[];"
    "for(const t of ['wheel','mousemove','mousedown','mouseup','click','keydown','keyup'])"
    "addEventListener(t,e=>__events.push({t,ts:e.timeStamp,trusted:e.isTrusted}),true);"
    "</script>"
)


def test_persistent_actions_produce_human_input_events(persistent_session_env: Path) -> None:
    name = "behavior"

    async def exercise() -> list[dict[str, Any]]:
        await browser_session.open_session(name)

        async def act(page: Any) -> list[dict[str, Any]]:
            await browser_session.goto(page, _BEHAVIOR_PAGE, "load")
            await browser_session.element_action(page, "click", "#go", [])
            await browser_session.element_action(page, "type", "#q", ["abc"])
            return await browser_session.evaluate(page, "window.__events")

        return await browser_session.run_page_action(name, act, timeout_ms=30_000)

    try:
        events = asyncio.run(exercise())
    finally:
        browser_session.close_session(name, delete_data=True)

    kinds = [event["t"] for event in events]
    assert all(event["trusted"] for event in events)
    assert kinds.count("wheel") >= 5  # scrolled to the button by wheel, not a jump
    first_down = kinds.index("mousedown")
    assert kinds[:first_down].count("mousemove") >= 10  # a path, not a teleport
    down, up = events[first_down], events[kinds.index("mouseup")]
    assert up["ts"] - down["ts"] >= 50  # a human button hold, not ~2 ms
    keydowns = [event["ts"] for event in events if event["t"] == "keydown"]
    assert len(keydowns) == 3
    assert all(
        later - earlier >= 50 for earlier, later in zip(keydowns, keydowns[1:], strict=False)
    )
