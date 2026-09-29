"""WebSkrap performance benchmarks.

Unlike a parser benchmark, WebSkrap is a Playwright-based browser automation
framework, so these benchmarks measure what WebSkrap actually does:

1. Resource routing  - ResourcePolicy.ALL vs LITE vs DOCUMENTS
2. Session reuse      - cold launch per fetch vs warm persistent session
3. Concurrency        - throughput when fetching many pages at once

All benchmarks run against a local HTTP server that serves a synthetic page
referencing many sub-resources (images, stylesheets, media). Each sub-resource
is answered after a small fixed delay to model real-world network cost, so the
effect of blocking resources is observable and repeatable. No external sites are
contacted. Timings vary with the host and browser version.

Sessions use the stealth setup with the Patchright driver. Three modes:

- virtual-display (default): headless=True with virtual_display=True, so
  Chromium runs headed on a private Xvfb screen WebSkrap starts per session.
  Needs Linux with Xvfb installed.
- headed: headless=False on the display in $DISPLAY. On a machine without a
  desktop, run it under xvfb-run.
- headless: native headless Chromium, without Xvfb.

Run:  python benchmarks.py
      python benchmarks.py --mode headed
      python benchmarks.py --mode headless --mask-headless-user-agent --repeat 5
Use --concurrency to set pages per batch and --warmup for untimed batches.
MCP runtime benchmarks measure the admission/reuse path, excluding JSON-RPC
transport, URL DNS checks, and result shaping.
If Chromium cannot use its OS sandbox on this host:
      WEBSKRAP_CHROMIUM_SANDBOX=0 python benchmarks.py
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import os
import sys
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from statistics import mean

from webskrap import ResourcePolicy, SessionConfig, WebSkrapClient
from webskrap.browser_session import sandbox_enabled
from webskrap.fetch_runtime import FetchRuntime
from webskrap.profiles import get_profile

# --- tunables -------------------------------------------------------------

ASSET_DELAY_S = 0.015  # simulated per-resource latency
N_IMAGES = 40
N_STYLES = 8
N_MEDIA = 6
WARMUP = 2
REPEAT = 20  # navigations averaged per benchmark
CONCURRENCY = 8  # pages for the concurrency benchmark

# --- synthetic target page ------------------------------------------------

_PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000002000154a24f900000000049454e44ae42"
    "6082"
)


def _build_page() -> str:
    images = "".join(f'<img src="/asset/img/{i}.png">' for i in range(N_IMAGES))
    styles = "".join(f'<link rel="stylesheet" href="/asset/css/{i}.css">' for i in range(N_STYLES))
    media = "".join(
        f'<video src="/asset/media/{i}.mp4" preload="auto"></video>' for i in range(N_MEDIA)
    )
    return (
        "<!doctype html><html><head><title>Benchmark</title>"
        f"{styles}</head><body>"
        + "".join(f'<div class="item">item {i}</div>' for i in range(200))
        + images
        + media
        + "</body></html>"
    )


_PAGE = _build_page().encode()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # silence access logs
        pass

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path == "/":
            body, ctype = _PAGE, "text/html"
        elif self.path.startswith("/asset/"):
            time.sleep(ASSET_DELAY_S)
            if self.path.startswith("/asset/css/"):
                body, ctype = b".item{color:#111}", "text/css"
            elif self.path.startswith("/asset/media/"):
                body, ctype = b"\x00\x00\x00\x18ftypmp42", "video/mp4"
            else:
                body, ctype = _PIXEL_PNG, "image/png"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        # A completed fetch can close its page while assets are pending.
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)


class LocalServer:
    def __init__(self) -> None:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        host, port = self._httpd.server_address
        return f"http://{host}:{port}/"

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=5)
        self._httpd.server_close()


# --- benchmark harness ----------------------------------------------------


def benchmark(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        name = func.__name__.replace("bench_", "").replace("_", " ")
        print(f"-> {name}", end=" ", flush=True)
        for _ in range(WARMUP):
            await func(*args, **kwargs)
        times = []
        for _ in range(REPEAT):
            started = time.perf_counter()
            await func(*args, **kwargs)
            times.append((time.perf_counter() - started) * 1000)
        avg = round(mean(times), 2)
        print(f"avg: {avg} ms")
        return avg

    return wrapper


MODE = "virtual-display"  # set from --mode
MASK_UA = False


def _config(policy: ResourcePolicy) -> SessionConfig:
    headed = MODE == "headed"
    return SessionConfig(
        driver="patchright",
        headless=not headed,
        virtual_display=MODE == "virtual-display",
        chromium_sandbox=sandbox_enabled(None),
        resource_policy=policy,
        decline_cookies=False,
        mask_headless_user_agent=MASK_UA,
    )


@benchmark
async def bench_policy(session) -> None:
    await session.fetch(URL, wait_until="load")


@benchmark
async def bench_cold_launch(client) -> None:
    await client.fetch(URL, config=_config(ResourcePolicy.LITE), wait_until="load")


@benchmark
async def bench_warm_session(session) -> None:
    await session.fetch(URL, wait_until="load")


@benchmark
async def bench_concurrent(session) -> None:
    await asyncio.gather(*(session.fetch(URL, wait_until="load") for _ in range(CONCURRENCY)))


@benchmark
async def bench_mcp_session(runtime) -> None:
    async with runtime.use("bench", _config(ResourcePolicy.LITE), get_profile(None)) as session:
        await session.fetch(URL, wait_until="load")


@benchmark
async def bench_mcp_concurrent(runtime) -> None:
    async def fetch_one() -> None:
        async with runtime.use("bench", _config(ResourcePolicy.LITE), get_profile(None)) as session:
            await session.fetch(URL, wait_until="load")

    await asyncio.gather(*(fetch_one() for _ in range(CONCURRENCY)))


def display(title: str, results: dict[str, float], baseline_key: str) -> None:
    baseline = results[baseline_key]
    ordered = sorted(results.items(), key=lambda kv: kv[1])
    print(f"\n{title}")
    print(f"{'config':<22} | {'avg (ms)':<10} | vs {baseline_key}")
    print("-" * 50)
    for name, value in ordered:
        ratio = round(value / baseline, 2)
        print(f"{name:<22} | {str(value):<10} | {ratio}x")
    print()


async def main() -> None:
    global URL
    with LocalServer() as URL:
        async with WebSkrapClient() as client:
            # 1. Resource routing
            policy_results: dict[str, float] = {}
            for label, policy in (
                ("ALL", ResourcePolicy.ALL),
                ("LITE", ResourcePolicy.LITE),
                ("DOCUMENTS", ResourcePolicy.DOCUMENTS),
            ):
                session = await client.session(f"policy_{label}", config=_config(policy))
                async with session:
                    policy_results[label] = await bench_policy(session)
            display(
                "Resource routing (full page load with delayed assets)",
                policy_results,
                "ALL",
            )

            # 2. Session reuse
            warm = await client.session("warm", config=_config(ResourcePolicy.LITE))
            reuse_results = {
                "cold launch / fetch": await bench_cold_launch(client),
                "warm session reuse": await bench_warm_session(warm),
            }
            display("Session reuse", reuse_results, "warm session reuse")
            await warm.close()

            # 3. Concurrency
            conc = await client.session("conc", config=_config(ResourcePolicy.LITE))
            per_page = round(await bench_concurrent(conc) / CONCURRENCY, 2)
            print(f"Concurrency: {CONCURRENCY} pages/batch, {per_page} ms per page\n")
            await conc.close()

            # Same admission/reuse path used by fetch and stealth_fetch over MCP.
            runtime = FetchRuntime()
            try:
                mcp_warm = await bench_mcp_session(runtime)
                display(
                    "MCP session reuse (driver/browser retained)",
                    {
                        "isolated cold fetch": reuse_results["cold launch / fetch"],
                        "MCP warm": mcp_warm,
                    },
                    "isolated cold fetch",
                )
                batch_ms = await bench_mcp_concurrent(runtime)
                print(f"MCP bounded concurrency: {CONCURRENCY * 1000 / batch_ms:.2f} pages/s\n")
            finally:
                await runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run WebSkrap performance benchmarks.")
    parser.add_argument(
        "--mode",
        choices=("virtual-display", "headed", "headless"),
        default=MODE,
        help="virtual-display: headed on private Xvfb; headed: current display; headless: native.",
    )
    parser.add_argument("--repeat", type=int, default=REPEAT, help="Timed batches per benchmark.")
    parser.add_argument("--warmup", type=int, default=WARMUP, help="Untimed warmup batches.")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY, help="Pages per batch.")
    parser.add_argument(
        "--mask-headless-user-agent", action="store_true", help="Measure UA probe/cache overhead."
    )
    args = parser.parse_args()
    if args.repeat < 1 or args.warmup < 0 or args.concurrency < 1:
        parser.error("repeat and concurrency must be positive; warmup must be nonnegative")
    if args.mask_headless_user_agent and args.mode != "headless":
        parser.error("--mask-headless-user-agent requires --mode headless")
    MODE, REPEAT, WARMUP, CONCURRENCY = args.mode, args.repeat, args.warmup, args.concurrency
    MASK_UA = args.mask_headless_user_agent
    if (
        MODE == "headed"
        and sys.platform.startswith("linux")
        and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    ):
        parser.error("headed mode needs a display; on a machine without one, use xvfb-run -a")
    print(f" WebSkrap performance benchmarks ({MODE})\n")
    asyncio.run(main())
