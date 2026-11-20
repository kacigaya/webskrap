from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_report_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "live_stealth_report.py"
    spec = importlib.util.spec_from_file_location("live_stealth_report", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_live_stealth_report_renders_summary_and_comparison() -> None:
    report = _load_report_module()
    results = [
        report.TestResult(
            "tests/live/test_bot_detection.py::test_alpha", "headed", "test_alpha", "passed", 1.2
        ),
        report.TestResult(
            "tests/live/test_bot_detection_headless.py::test_alpha_headless",
            "headless",
            "test_alpha_headless",
            "failed",
            0.8,
            "boom",
        ),
    ]

    payload = report.build_payload(results, 1.0, 3.0)
    html = report.render_html(payload)

    assert payload["summary"]["overall"] == {"passed": 1, "failed": 1}
    assert "Headed vs Headless" in html
    assert "test_alpha" in html
    assert "boom" in html


def test_live_stealth_report_report_only_exits_zero(monkeypatch, tmp_path: Path) -> None:
    report = _load_report_module()
    monkeypatch.setattr(
        report,
        "run_pytest",
        lambda: (
            1,
            [
                report.TestResult(
                    "tests/live/test_bot_detection.py::test_alpha",
                    "headed",
                    "test_alpha",
                    "failed",
                    0.1,
                )
            ],
            1.0,
            2.0,
        ),
    )
    monkeypatch.setattr(
        report,
        "parse_args",
        lambda: type(
            "Args",
            (),
            {
                "json": tmp_path / "report.json",
                "html": tmp_path / "report.html",
                "no_open": True,
                "report_only": True,
            },
        )(),
    )

    assert report.main() == 0
