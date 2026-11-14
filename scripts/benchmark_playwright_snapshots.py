"""Compare Playwright MCP on the same page and three-step edit task.

Run outside the production dependency graph:
uv run --with tiktoken python scripts/benchmark_playwright_snapshots.py
Requires npx; installs @playwright/mcp@0.0.85 in npm's external cache.
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import tempfile
import time
from pathlib import Path

import tiktoken
from benchmark_snapshots import task_html, usage
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from webskrap.cdp import find_executable


async def run():
    encoding = tiktoken.get_encoding("o200k_base")
    results = []
    with tempfile.TemporaryDirectory(prefix="webskrap-pw-benchmark-") as directory:
        fixture = Path(directory) / "task.html"
        fixture.write_text(task_html())
        for mode in ("full", "none"):
            for repeat in range(3):
                params = StdioServerParameters(
                    command="npx",
                    cwd=directory,
                    args=[
                        "--yes",
                        "@playwright/mcp@0.0.85",
                        "--headless",
                        "--no-sandbox",
                        "--executable-path",
                        str(find_executable("chromium")),
                        "--allow-unrestricted-file-access",
                        "--snapshot-mode",
                        mode,
                    ],
                )
                async with (
                    stdio_client(params) as (read, write),
                    ClientSession(read, write) as client,
                ):
                    await client.initialize()
                    await client.call_tool("browser_navigate", {"url": fixture.as_uri()})
                    requests = []
                    responses = []
                    latencies = []
                    before = usage()
                    started = time.perf_counter()

                    async def call(
                        name,
                        arguments,
                        requests=requests,
                        responses=responses,
                        latencies=latencies,
                        client=client,
                    ):
                        requests.append(json.dumps({"tool": name, **arguments}))
                        start = time.perf_counter()
                        result = await client.call_tool(name, arguments)
                        text = "\n".join(
                            block.text for block in result.content if block.type == "text"
                        )
                        if result.is_error:
                            raise RuntimeError(text)
                        responses.append(text)
                        if name == "browser_find":
                            latencies.append((time.perf_counter() - start) * 1000)
                        return text

                    for label, action in (
                        ("Edit record 179", "browser_click"),
                        ("Title", "browser_type"),
                        ("Save changes", "browser_click"),
                    ):
                        text = await call("browser_find", {"text": label})
                        ref = re.search(
                            r'(?:button|textbox) "' + re.escape(label) + r'"[^\n]*?\[ref=([^]]+)\]',
                            text,
                        )[1]
                        arguments = {"target": ref}
                        if action == "browser_type":
                            arguments["text"] = "Updated"
                        await call(action, arguments)
                    result = await client.call_tool(
                        "browser_evaluate",
                        {"function": "() => document.querySelector('#result').textContent"},
                    )
                    success = any(
                        "Saved Updated" in block.text
                        for block in result.content
                        if block.type == "text"
                    )
                    after = usage()
                    results.append(
                        {
                            "mode": mode,
                            "repeat": repeat,
                            "success": success,
                            "tool_calls": len(requests),
                            "request_tokens": sum(len(encoding.encode(x)) for x in requests),
                            "response_tokens": sum(len(encoding.encode(x)) for x in responses),
                            "response_bytes": sum(len(x.encode()) for x in responses),
                            "snapshot_ms_median": statistics.median(latencies),
                            "task_ms": (time.perf_counter() - started) * 1000,
                            "cpu_seconds": after["cpu_seconds"] - before["cpu_seconds"],
                            "rss_mib_after": after["rss_mib"],
                        }
                    )
    return {"package": "@playwright/mcp@0.0.85", "runs": results}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), indent=2))
