"""Compare automation signals and timing of the Patchright and CDP drivers.

Serves one local probe page whose own (main-world) script records signals that
detectors read, then fetches it with each driver and prints a table. Nothing
leaves the machine unless ``--url`` names a public page, in which case only
status, title and timing are compared.

Run:  python scripts/compare_drivers.py
      python scripts/compare_drivers.py --mode virtual-display --repeat 5
      python scripts/compare_drivers.py --url https://example.com
If Chromium cannot use its OS sandbox on this host:
      WEBSKRAP_CHROMIUM_SANDBOX=0 python scripts/compare_drivers.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from webskrap import SessionConfig, WebSkrapClient
from webskrap.browser_session import sandbox_enabled
from webskrap.cdp_fetch import fetch as cdp_fetch
from webskrap.urls import PRIVATE_NET_ENV

# The probe writes its findings into <pre id=out> as JSON. The console probe
# is the classic Runtime.enable test: a driver that enabled Runtime or Console
# serializes logged errors, which reads the `stack` getter.
PROBE_PAGE = b"""<!doctype html><html><head><title>probe</title></head><body>
<pre id="out"></pre>
<script>
(async () => {
  let consoleTouched = false;
  const bait = new Error('bait');
  Object.defineProperty(bait, 'stack', { get() { consoleTouched = true; return ''; } });
  console.debug(bait);
  await new Promise((resolve) => setTimeout(resolve, 200));
  let webgl = null;
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const info = gl && gl.getExtension('WEBGL_debug_renderer_info');
    webgl = info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : null;
  } catch (error) { webgl = 'error'; }
  let brands = null;
  if (navigator.userAgentData) brands = navigator.userAgentData.brands.map((b) => b.brand);
  document.getElementById('out').textContent = JSON.stringify({
    webdriver: navigator.webdriver,
    console_probe_touched: consoleTouched,
    headless_ua: /Headless/.test(navigator.userAgent),
    ua_ch_brands: brands,
    window_chrome: typeof window.chrome,
    plugins: navigator.plugins.length,
    languages: navigator.languages,
    screen: [screen.width, screen.height],
    outer: [outerWidth, outerHeight],
    webgl_renderer: webgl,
    playwright_globals: Object.keys(window).filter((k) => /playwright|__pw|cdc_/i.test(k)),
  });
})();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PROBE_PAGE)

    def log_message(self, *_args: object) -> None:
        return


def _config(mode: str, driver: str) -> SessionConfig:
    return SessionConfig(
        driver="patchright" if driver == "patchright" else "playwright",
        channel="chromium",
        headless=mode != "headed",
        virtual_display=mode == "virtual-display",
        chromium_sandbox=sandbox_enabled(),
    )


async def _run(driver: str, mode: str, url: str) -> tuple[float, str]:
    started = time.perf_counter()
    kwargs = {"text_only": True, "wait_until": "load", "ready_selector": "#out:not(:empty)"}
    if driver == "cdp":
        result = await cdp_fetch(url, config=_config(mode, driver), **kwargs)
    else:
        async with WebSkrapClient() as client:
            result = await client.fetch(url, config=_config(mode, driver), **kwargs)
    return (time.perf_counter() - started) * 1000, result.text


async def _run_public(driver: str, mode: str, url: str) -> tuple[float, str]:
    started = time.perf_counter()
    if driver == "cdp":
        result = await cdp_fetch(url, config=_config(mode, driver), text_only=True)
    else:
        async with WebSkrapClient() as client:
            result = await client.fetch(url, config=_config(mode, driver), text_only=True)
    summary = json.dumps({"status": result.status, "title": result.title})
    return (time.perf_counter() - started) * 1000, summary


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("headless", "virtual-display", "headed"))
    parser.add_argument("--repeat", type=int, default=3, help="Timed fetches per driver.")
    parser.add_argument("--url", help="Public page to compare instead of the local probe.")
    args = parser.parse_args()
    mode = args.mode or "headless"

    server = None
    if args.url:
        url, run = args.url, _run_public
    else:
        os.environ[PRIVATE_NET_ENV] = "1"
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url, run = f"http://127.0.0.1:{server.server_port}/", _run

    try:
        for driver in ("patchright", "cdp"):
            timings = []
            text = ""
            for _ in range(max(1, args.repeat)):
                elapsed, text = await run(driver, mode, url)
                timings.append(elapsed)
            print(f"== {driver} ({mode}) median {statistics.median(timings):.0f} ms")
            try:
                for key, value in json.loads(text.strip()).items():
                    print(f"   {key:22} {value}")
            except json.JSONDecodeError:
                print(f"   {text[:300]}")
    finally:
        if server is not None:
            server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
