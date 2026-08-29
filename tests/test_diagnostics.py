from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from webskrap import diagnostics
from webskrap.paths import MCP_PROFILE_DIR_ENV, OUTPUT_DIR_ENV


def _stub_probe(monkeypatch: Any, **overrides: Any) -> None:
    async def fake_doctor() -> dict[str, Any]:
        return {
            "ok": True,
            "message": "ready",
            "driver": "patchright",
            "channel": "chrome",
            "executable_path": "/browsers/chrome",
            **overrides,
        }

    monkeypatch.setattr(diagnostics, "browser_doctor", fake_doctor)


def test_diagnose_keeps_the_launch_probe_and_adds_context(monkeypatch: Any, tmp_path: Path) -> None:
    _stub_probe(monkeypatch)
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv(OUTPUT_DIR_ENV, str(tmp_path / "out"))
    monkeypatch.setenv(MCP_PROFILE_DIR_ENV, str(tmp_path / "profiles"))

    report = asyncio.run(diagnostics.diagnose())

    assert report["ok"] is True
    assert report["message"] == "ready"
    assert report["executable_path"] == "/browsers/chrome"
    assert report["versions"]["webskrap"]
    assert report["versions"]["python"]
    assert report["paths"] == {
        "sessions_root": str(tmp_path / "sessions"),
        "output_root": str(tmp_path / "out"),
        "mcp_profile_root": str(tmp_path / "profiles"),
    }
    assert report["sessions"] == []


def test_diagnose_reports_unset_overrides_as_none(monkeypatch: Any) -> None:
    _stub_probe(monkeypatch)
    for name in diagnostics.ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    report = asyncio.run(diagnostics.diagnose())

    assert set(report["environment"]) == set(diagnostics.ENVIRONMENT_VARIABLES)
    assert all(value is None for value in report["environment"].values())
    # Unset means sandboxed, which is what the resolved value must show.
    assert report["chromium_sandbox"] is True


def test_diagnose_lists_sessions_even_when_the_browser_is_broken(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _stub_probe(monkeypatch, ok=False, message="did not launch", hint="Run: webskrap install")
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path))
    (tmp_path / "shop" / "user-data").mkdir(parents=True)

    report = asyncio.run(diagnostics.diagnose())

    assert report["ok"] is False
    assert report["hint"] == "Run: webskrap install"
    assert report["sessions"] == [
        {"session": "shop", "running": False, "pid": None, "port": None, "chromium_sandbox": None}
    ]


def test_package_version_returns_none_for_an_absent_distribution() -> None:
    assert diagnostics.package_version("webskrap-does-not-exist") is None


@pytest.mark.browser
def test_diagnose_probes_a_real_browser() -> None:
    report = asyncio.run(diagnostics.diagnose())

    assert report["ok"] is True
    assert report["channel"] in ("chrome", "chromium")
    assert Path(str(report["executable_path"])).exists()
