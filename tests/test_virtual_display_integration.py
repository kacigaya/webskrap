"""Real headed Chromium on this host's virtual display backend.

Skipped where :func:`webskrap.display.available` is False, unless
``WEBSKRAP_REQUIRE_VIRTUAL_DISPLAY=1`` turns that into a failure, as the CI
virtual display jobs do. With ``WEBSKRAP_VIRTUAL_DISPLAY_REPORT`` set to a
directory, each test writes what the page saw there as JSON.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from webskrap import SessionConfig, WebSkrapClient, browser_session, display
from webskrap.client import lavapipe_available
from webskrap.models import Viewport

pytestmark = pytest.mark.browser

_REQUIRED = os.environ.get("WEBSKRAP_REQUIRE_VIRTUAL_DISPLAY") == "1"

_PROBE = """async () => {
    const gl = document.createElement('canvas').getContext('webgl');
    const info = gl && gl.getExtension('WEBGL_debug_renderer_info');
    let glPixel = null;
    if (gl) {
        gl.clearColor(0, 1, 0, 1);
        gl.clear(gl.COLOR_BUFFER_BIT);
        const pixel = new Uint8Array(4);
        gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
        glPixel = Array.from(pixel);
    }
    const canvas = document.createElement('canvas');
    const context = canvas.getContext('2d');
    context.fillStyle = 'rgb(255, 0, 0)';
    context.fillRect(0, 0, 4, 4);
    let gpu = null;
    if (navigator.gpu) {
        const adapter = await navigator.gpu.requestAdapter().catch(() => null);
        gpu = {adapter: null};
        if (adapter) {
            const device = await adapter.requestDevice();
            const texture = device.createTexture({size: [1, 1], format: 'rgba8unorm',
                usage: GPUTextureUsage.RENDER_ATTACHMENT | GPUTextureUsage.COPY_SRC});
            const buffer = device.createBuffer({size: 256,
                usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ});
            const encoder = device.createCommandEncoder();
            const pass = encoder.beginRenderPass({colorAttachments: [{view: texture.createView(),
                clearValue: {r: 0, g: 0, b: 1, a: 1}, loadOp: 'clear', storeOp: 'store'}]});
            pass.end();
            encoder.copyTextureToBuffer({texture}, {buffer, bytesPerRow: 256}, [1, 1]);
            device.queue.submit([encoder.finish()]);
            await buffer.mapAsync(GPUMapMode.READ);
            gpu = {vendor: adapter.info?.vendor ?? '',
                architecture: adapter.info?.architecture ?? '',
                pixel: Array.from(new Uint8Array(buffer.getMappedRange()).slice(0, 4))};
            buffer.unmap();
            buffer.destroy();
            texture.destroy();
            device.destroy();
        }
    }
    return {
        ua: navigator.userAgent,
        brands: navigator.userAgentData.brands.map((b) => b.brand),
        hints: await navigator.userAgentData.getHighEntropyValues(
            ['architecture', 'bitness', 'platformVersion', 'uaFullVersion']),
        webdriver: navigator.webdriver,
        screen: [screen.width, screen.height],
        available: [screen.availWidth, screen.availHeight],
        extended: screen.isExtended ?? null,
        window: [outerWidth, outerHeight, screenX, screenY],
        dpr: devicePixelRatio,
        hover: matchMedia('(hover: hover)').matches,
        finePointer: matchMedia('(pointer: fine)').matches,
        visibility: document.visibilityState,
        webglUnmasked: Boolean(info),
        webglVendor: info ? gl.getParameter(info.UNMASKED_VENDOR_WEBGL) :
            gl ? gl.getParameter(gl.VENDOR) : null,
        webglRenderer: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) :
            gl ? gl.getParameter(gl.RENDERER) : null,
        glPixel,
        canvasPixel: Array.from(context.getImageData(1, 1, 1, 1).data),
        webgpu: gpu,
    };
}"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<!doctype html><title>Display</title><p>virtual display</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture(scope="module")
def page_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def _native_displays() -> object:
    if sys.platform == "darwin":
        from webskrap.display import _macos

        runtime = _macos._Runtime()
        ids = (ctypes.c_uint32 * 32)()
        count = ctypes.c_uint32()
        assert runtime.cg.CGGetOnlineDisplayList(32, ids, ctypes.byref(count)) == 0
        return sorted(ids[: count.value])
    if sys.platform == "win32":
        from webskrap.display import _windows

        result = []
        for monitor in _windows._monitors():
            mode = _windows._mode(monitor.name)
            result.append(
                (
                    monitor.name,
                    monitor.attached,
                    None if mode is None else (mode.x, mode.y, mode.dmPelsWidth, mode.dmPelsHeight),
                )
            )
        return sorted(result)
    return None


@pytest.fixture
def backend() -> Iterator[None]:
    if not display.available():
        if _REQUIRED:
            pytest.fail(f"no virtual display backend is available on {sys.platform}")
        pytest.skip("no virtual display backend on this host")
    before = _native_displays()
    yield
    deadline = time.monotonic() + 10
    while _native_displays() != before:
        assert time.monotonic() < deadline, "native virtual display state was not restored"
        time.sleep(0.05)


def _report(name: str, seen: dict[str, Any]) -> None:
    directory = os.environ.get("WEBSKRAP_VIRTUAL_DISPLAY_REPORT")
    if directory:
        path = Path(directory) / f"{sys.platform}-{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen, indent=2), encoding="utf-8")


def _assert_headed_and_rendering(seen: dict[str, Any], width: int, height: int) -> None:
    assert "HeadlessChrome" not in seen["ua"]
    assert "HeadlessChrome" not in seen["brands"]
    assert seen["webdriver"] is False
    assert seen["hints"]["architecture"]
    assert seen["hints"]["uaFullVersion"]
    assert seen["screen"] == [width, height]
    assert seen["hover"] is True
    assert seen["finePointer"] is True
    assert seen["visibility"] == "visible"
    assert seen["canvasPixel"] == [255, 0, 0, 255]
    assert seen["webglRenderer"], "WebGL is unavailable on the virtual display"
    assert seen["glPixel"] == [0, 255, 0, 255]
    if sys.platform.startswith("linux") and lavapipe_available():
        assert "SwiftShader" not in seen["webglRenderer"]
    if seen["webgpu"] and "pixel" in seen["webgpu"]:
        assert seen["webgpu"]["pixel"] == [0, 0, 255, 255]
    if sys.platform in ("darwin", "win32"):
        assert seen["webgpu"] is not None, "navigator.gpu is missing"


def _gone(directory: Path) -> bool:
    return not directory.exists()


async def _probe(session: Any, url: str) -> dict[str, Any]:
    page = await session.new_page()
    await page.goto(url, wait_until="load", timeout_s=30)
    return await page.evaluate(_PROBE)


async def test_session_runs_headed_with_webgl_at_the_requested_size(
    backend: None, page_url: str, sandbox_supported: bool
) -> None:
    config = SessionConfig(
        chromium_sandbox=sandbox_supported,
        channel="chromium",
        virtual_display=True,
        headless_screen=Viewport(width=1280, height=720),
    )
    async with WebSkrapClient(default_config=config) as client:
        session = await client.session("virtual-display", config=config)
        virtual = session._display
        assert virtual is not None
        seen = await _probe(session, page_url)
        directory = virtual.directory
    _report("single", {**seen, "origin": list(virtual.origin)})

    _assert_headed_and_rendering(seen, 1280, 720)
    assert _gone(directory)
    assert virtual._process.poll() is not None


async def test_concurrent_sessions_get_separate_displays(
    backend: None, page_url: str, sandbox_supported: bool
) -> None:
    sizes = [(1280, 720), (1366, 768)]
    configs = [
        SessionConfig(
            chromium_sandbox=sandbox_supported,
            channel="chromium",
            virtual_display=True,
            headless_screen=Viewport(width=width, height=height),
        )
        for width, height in sizes
    ]
    async with WebSkrapClient(default_config=configs[0]) as client:
        sessions = await asyncio.gather(
            *(client.session(f"concurrent-{i}", config=c) for i, c in enumerate(configs))
        )
        virtuals = [s._display for s in sessions if s._display is not None]
        directories = [v.directory for v in virtuals]
        seen = await asyncio.gather(*(_probe(s, page_url) for s in sessions))
    _report("concurrent", {"sessions": seen})

    assert len(set(directories)) == 2
    for result, (width, height) in zip(seen, sizes, strict=True):
        _assert_headed_and_rendering(result, width, height)
    assert all(_gone(directory) for directory in directories)
    assert all(v._process.poll() is not None for v in virtuals)


def test_persistent_session_keeps_its_display_and_recovers_after_a_crash(
    backend: None, persistent_session_env: Path, page_url: str
) -> None:
    name = "virtual-display-persistent"

    async def probe() -> dict[str, Any]:
        async def action(page: Any) -> dict[str, Any]:
            await page.goto(page_url, wait_until="load", timeout_s=30)
            return await browser_session.evaluate(page, _PROBE)

        return await browser_session.run_page_action(name, action)

    try:
        opened = asyncio.run(browser_session.open_session(name, virtual_display=True))
        state = browser_session.read_state(browser_session.session_dir(name))
        assert state is not None
        first_display = Path(state["display_dir"])
        seen = asyncio.run(probe())
        _report("persistent", seen)
        _assert_headed_and_rendering(seen, 1920, 1080)
        assert opened["virtual_display"] is True

        browser_session.kill_browser(opened["pid"], force=True)
        deadline = time.monotonic() + 10
        while browser_session.session_running(browser_session.session_dir(name), state):
            assert time.monotonic() < deadline, "killed browser did not exit"
            time.sleep(0.02)
        reopened = asyncio.run(browser_session.open_session(name, virtual_display=True))
        state = browser_session.read_state(browser_session.session_dir(name))
        assert state is not None
        second_display = Path(state["display_dir"])

        assert reopened["reused"] is False
        assert _gone(first_display)
        assert second_display.exists()
        _assert_headed_and_rendering(asyncio.run(probe()), 1920, 1080)
    finally:
        browser_session.close_session(name)

    assert _gone(second_display)
