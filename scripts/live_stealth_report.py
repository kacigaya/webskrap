from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = ROOT / ".webskrap" / "reports"
DEFAULT_JSON = DEFAULT_REPORT_DIR / "live-stealth-results.json"
DEFAULT_HTML = DEFAULT_REPORT_DIR / "live-stealth-results.html"
SUITES = (
    ROOT / "tests" / "test_bot_detection.py",
    ROOT / "tests" / "test_bot_detection_headless.py",
)


@dataclass
class TestResult:
    nodeid: str
    suite: str
    name: str
    outcome: str
    duration: float
    failure: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "nodeid": self.nodeid,
            "suite": self.suite,
            "name": self.name,
            "outcome": self.outcome,
            "duration": round(self.duration, 3),
            "failure": self.failure,
        }


class ResultCollector:
    def __init__(self) -> None:
        self._reports: dict[str, list[Any]] = defaultdict(list)

    def pytest_runtest_logreport(self, report: Any) -> None:
        self._reports[report.nodeid].append(report)

    def results(self) -> list[TestResult]:
        results: list[TestResult] = []
        for nodeid, reports in sorted(self._reports.items()):
            duration = sum(float(report.duration) for report in reports)
            report = next((r for r in reports if r.failed), None)
            report = report or next((r for r in reports if r.skipped), None)
            report = report or next((r for r in reports if r.when == "call"), reports[-1])
            results.append(
                TestResult(
                    nodeid=nodeid,
                    suite=suite_for_nodeid(nodeid),
                    name=nodeid.rsplit("::", 1)[-1],
                    outcome=str(report.outcome),
                    duration=duration,
                    failure=longrepr_text(report),
                )
            )
        return results


def longrepr_text(report: Any) -> str:
    if not getattr(report, "failed", False):
        return ""
    return str(getattr(report, "longrepr", ""))[-2000:]


def suite_for_nodeid(nodeid: str) -> str:
    path = nodeid.split("::", 1)[0].replace("\\", "/")
    if path.endswith("test_bot_detection_headless.py"):
        return "headless"
    if path.endswith("test_bot_detection.py"):
        return "headed"
    return "other"


def summarize(results: list[TestResult]) -> dict[str, Any]:
    overall = Counter(result.outcome for result in results)
    by_suite: dict[str, dict[str, int]] = {}
    for suite in sorted({result.suite for result in results}):
        counts = Counter(result.outcome for result in results if result.suite == suite)
        by_suite[suite] = dict(counts)
    return {"overall": dict(overall), "by_suite": by_suite}


def comparison_rows(results: list[TestResult]) -> list[dict[str, str]]:
    pairs: dict[str, dict[str, str]] = defaultdict(dict)
    for result in results:
        if result.suite not in {"headed", "headless"}:
            continue
        name = result.name.removesuffix("_headless")
        pairs[name][result.suite] = result.outcome
    return [
        {"test": name, "headed": pair.get("headed", "-"), "headless": pair.get("headless", "-")}
        for name, pair in sorted(pairs.items())
    ]


def bar_chart(title: str, values: dict[str, int], colors: dict[str, str] | None = None) -> str:
    colors = colors or {}
    width = 640
    row_height = 28
    label_width = 190
    max_value = max(values.values(), default=1)
    height = 44 + row_height * max(1, len(values))
    rows = [f'<h2>{escape(title)}</h2><svg viewBox="0 0 {width} {height}" role="img">']
    rows.append(f'<text x="0" y="18" class="chart-title">{escape(title)}</text>')
    for index, (label, value) in enumerate(values.items()):
        y = 34 + index * row_height
        bar_width = int((width - label_width - 60) * (value / max_value)) if value else 0
        color = colors.get(label, "#3b82f6")
        rows.append(f'<text x="0" y="{y + 15}">{escape(label)}</text>')
        rows.append(
            f'<rect x="{label_width}" y="{y}" width="{bar_width}" height="18" fill="{color}" />'
        )
        rows.append(f'<text x="{label_width + bar_width + 8}" y="{y + 15}">{value}</text>')
    rows.append("</svg>")
    return "\n".join(rows)


def render_html(payload: dict[str, Any]) -> str:
    results = [TestResult(**item) for item in payload["tests"]]
    summary = payload["summary"]
    colors = {"passed": "#15803d", "failed": "#b91c1c", "skipped": "#a16207"}
    suite_values = {
        suite: sum(counts.values()) for suite, counts in summary.get("by_suite", {}).items()
    }
    rows = "\n".join(
        "<tr>"
        f"<td>{escape(row['test'])}</td>"
        f"<td class='{escape(row['headed'])}'>{escape(row['headed'])}</td>"
        f"<td class='{escape(row['headless'])}'>{escape(row['headless'])}</td>"
        "</tr>"
        for row in comparison_rows(results)
    )
    failures = "\n".join(
        "<details><summary>"
        f"{escape(result.suite)} / {escape(result.name)}"
        "</summary><pre>"
        f"{escape(result.failure)}"
        "</pre></details>"
        for result in results
        if result.outcome == "failed"
    )
    failures = failures or "<p>No failures captured.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>WebSkrap Live Stealth Results</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 32px; color: #172033; }}
h1 {{ margin-bottom: 4px; }}
h2 {{ margin-top: 28px; }}
.meta {{ color: #526070; }}
svg {{ width: 100%; max-width: 760px; height: auto; display: block; margin: 8px 0 20px; }}
text {{ font-size: 13px; fill: #172033; }}
.chart-title {{ font-weight: 700; }}
table {{ border-collapse: collapse; width: 100%; max-width: 920px; }}
th, td {{ border-bottom: 1px solid #d9e0ea; padding: 8px; text-align: left; }}
.passed {{ color: #15803d; font-weight: 700; }}
.failed {{ color: #b91c1c; font-weight: 700; }}
.skipped {{ color: #a16207; font-weight: 700; }}
pre {{ white-space: pre-wrap; background: #f6f8fb; padding: 12px; overflow: auto; }}
</style>
</head>
<body>
<h1>WebSkrap Live Stealth Results</h1>
<p class="meta">
Started {escape(payload["started_at"])}; duration {payload["duration_seconds"]:.1f}s.
</p>
{bar_chart("Overall Outcomes", summary.get("overall", {}), colors)}
{bar_chart("Tests Per Suite", suite_values)}
<h2>Headed vs Headless</h2>
<table>
<thead><tr><th>Test</th><th>Headed</th><th>Headless</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<h2>Failures</h2>
{failures}
</body>
</html>
"""


def build_payload(results: list[TestResult], started: float, finished: float) -> dict[str, Any]:
    return {
        "started_at": datetime.fromtimestamp(started, UTC).isoformat(),
        "finished_at": datetime.fromtimestamp(finished, UTC).isoformat(),
        "duration_seconds": round(finished - started, 3),
        "environment": {
            "browser_channel": os.environ.get("WEBSKRAP_BROWSER_CHANNEL", "chrome"),
            "headed_profile_dir": os.environ.get(
                "WEBSKRAP_LIVE_PROFILE_DIR", ".webskrap/live-stealth-profile"
            ),
            "headless_profile_dir": os.environ.get(
                "WEBSKRAP_LIVE_HEADLESS_PROFILE_DIR", ".webskrap/live-headless-profile"
            ),
            "proxy": bool(os.environ.get("WEBSKRAP_LIVE_PROXY")),
            "headless_virtual_display": os.environ.get("WEBSKRAP_LIVE_VIRTUAL_DISPLAY") == "1",
        },
        "tests": [result.to_json() for result in results],
        "summary": summarize(results),
    }


def run_pytest() -> tuple[int, list[TestResult], float, float]:
    os.environ["WEBSKRAP_LIVE"] = "1"
    os.environ["PYTHONPATH"] = str(ROOT / "src")
    sys.path.insert(0, str(ROOT / "src"))
    collector = ResultCollector()
    started = time.time()
    code = pytest.main(["-q", *(str(path) for path in SUITES)], plugins=[collector])
    finished = time.time()
    return int(code), collector.results(), started, finished


def write_report(json_path: Path, html_path: Path, payload: dict[str, Any]) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    html_path.write_text(render_html(payload), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live stealth tests and graph the results.")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON, help="JSON output path.")
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML, help="HTML output path.")
    parser.add_argument("--no-open", action="store_true", help="Do not open the HTML report.")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Write reports and exit 0 even if live tests fail.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    code, results, started, finished = run_pytest()
    payload = build_payload(results, started, finished)
    write_report(args.json, args.html, payload)
    print(f"Wrote {args.json}")
    print(f"Wrote {args.html}")
    if not args.no_open:
        webbrowser.open(args.html.resolve().as_uri())
    if args.report_only:
        return 0
    return code


if __name__ == "__main__":
    raise SystemExit(main())
