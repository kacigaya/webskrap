"""Measure a local edit/save task with old snapshots and targeted search.

Run: uv run --with tiktoken python scripts/benchmark_snapshots.py --baseline SOURCE
SOURCE is cdp_dom.py from the revision before this change. No Node is required.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import tiktoken

from webskrap import browser_session, cdp_dom
from webskrap.cdp import find_executable, launch


def usage():
    entries = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            entries[int(path.parent.name)] = (
                int(fields[1]),
                int(fields[11]) + int(fields[12]),
                int(fields[21]),
            )
        except (OSError, ValueError, IndexError):
            continue
    selected = {os.getpid()}
    while True:
        children = {pid for pid, (parent, _, _) in entries.items() if parent in selected}
        if children <= selected:
            break
        selected.update(children)
    return {
        "cpu_seconds": sum(entries[pid][1] for pid in selected if pid in entries)
        / os.sysconf("SC_CLK_TCK"),
        "rss_mib": sum(entries[pid][2] for pid in selected if pid in entries)
        * os.sysconf("SC_PAGE_SIZE")
        / 2**20,
    }


def task_html():
    """Return the identical local task page for each driver."""
    return (
        "<title>Records</title><main>"
        + "".join(
            f"<article><h2>Record {i}</h2><p>Project record details and history.</p>"
            f'<button onclick="edit()">Edit record {i}</button></article>'
            for i in range(180)
        )
        + """</main><dialog id="editor"><label>Title<input id="title"></label>
    <button onclick="save()">Save changes</button></dialog><div role="status" id="result"></div>
    <script>function edit(){editor.showModal()}
    function save(){result.textContent='Saved '+title.value;editor.close()}</script>"""
    )


async def run(source, repeats):
    spec = importlib.util.spec_from_file_location("webskrap._snapshot_baseline", source)
    assert spec is not None and spec.loader is not None
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[baseline.__name__] = baseline
    spec.loader.exec_module(baseline)
    encoding = tiktoken.get_encoding("o200k_base")
    html = task_html()
    results = []
    for repeat in range(repeats):
        for mode in ("baseline", "snapshot", "find"):
            browser = await launch(find_executable("chromium"), args=["--no-sandbox"])
            try:
                page = await browser.new_page()
                await page.enable_events()
                await page.send(
                    "Page.setDocumentContent", {"frameId": page.target_id, "html": html}
                )
                before = usage()
                started = time.perf_counter()
                latencies = []
                responses = []
                requests = []
                methods = []
                original_send = page.send

                async def send(method, params=None, methods=methods, original_send=original_send):
                    methods.append(method)
                    return await original_send(method, params)

                page.send = send
                for label, action, values in (
                    ("Edit record 179", "click", []),
                    ("Title", "fill", ["Updated"]),
                    ("Save changes", "click", []),
                ):
                    offset = 0
                    while True:
                        request = (
                            {"tool": "browser_find", "text": label}
                            if mode == "find"
                            else {"tool": "browser_snapshot", "offset": offset}
                        )
                        requests.append(json.dumps(request))
                        start = time.perf_counter()
                        if mode == "find":
                            pattern = re.compile("(?i)" + re.escape(label))
                            tree = await cdp_dom.snapshot(page, pattern=pattern)
                            tree, total = await cdp_dom.matching_snapshot(tree, pattern, 20)
                            payload = {
                                **await browser_session.page_state(page),
                                "snapshot": tree,
                                "matches_total": total,
                                "matches_truncated": total > 20,
                            }
                        else:
                            walker = baseline if mode == "baseline" else cdp_dom
                            payload = {
                                **await browser_session.page_state(page),
                                "snapshot": await walker.snapshot(page, depth=6),
                            }
                        payload = browser_session.shape_snapshot(payload, 8000, offset)
                        latencies.append((time.perf_counter() - start) * 1000)
                        responses.append(json.dumps(payload, ensure_ascii=False))
                        match = re.search(
                            r'- (?:button|textbox) "'
                            + re.escape(label)
                            + r'"[^\n]*?\[ref=([^]]+)\]',
                            payload["snapshot"],
                        )
                        if match:
                            break
                        offset = payload["next_snapshot_offset"]
                        if offset is None:
                            raise RuntimeError(f"Target missing: {label}")
                    ref = match[1]
                    requests.append(
                        json.dumps(
                            {
                                "tool": "browser_interact",
                                "action": action,
                                "target": ref,
                                "values": values,
                            }
                        )
                    )
                    await browser_session.element_action(page, action, ref, values)
                    responses.append(json.dumps(await browser_session.page_state(page)))
                success = (
                    await page.evaluate("document.querySelector('#result').textContent")
                    == "Saved Updated"
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
                        "describe_calls": methods.count("DOM.describeNode"),
                        "stealth_sensitive_calls": sum(
                            method
                            in {
                                "Runtime.enable",
                                "Console.enable",
                                "Accessibility.getFullAXTree",
                                "Accessibility.enable",
                            }
                            for method in methods
                        ),
                    }
                )
                print(f"{mode} run {repeat}: success={success}", file=sys.stderr)
            finally:
                await browser.close()
    return {"tokenizer": "tiktoken " + tiktoken.__version__ + " o200k_base", "runs": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.baseline, args.repeats)), indent=2))
