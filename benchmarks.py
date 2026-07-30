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
contacted, so results are deterministic and carry no terms-of-service concerns.

Run:  python benchmarks.py
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from statistics import mean

from webskrap import ResourcePolicy, SessionConfig, WebSkrapClient

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


def _config(policy: ResourcePolicy) -> SessionConfig:
    return SessionConfig(headless=True, resource_policy=policy, decline_cookies=False)


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

            # 3. Concurrency
            conc = await client.session("conc", config=_config(ResourcePolicy.LITE))
            per_page = round(await bench_concurrent(conc) / CONCURRENCY, 2)
            print(f"Concurrency: {CONCURRENCY} pages/batch, {per_page} ms per page\n")


if __name__ == "__main__":
    print(" WebSkrap performance benchmarks\n")
    asyncio.run(main())
