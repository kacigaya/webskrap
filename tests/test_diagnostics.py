from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from webskrap import browser_session, diagnostics
from webskrap.paths import MCP_PROFILE_DIR_ENV, OUTPUT_DIR_ENV


def _stub_probe(monkeypatch: Any, **overrides: Any) -> None:
    async def fake_doctor(**_kwargs: Any) -> dict[str, Any]:
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
    monkeypatch.setattr(diagnostics, "font_count", lambda: 17)
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv(OUTPUT_DIR_ENV, str(tmp_path / "out"))
    monkeypatch.setenv(MCP_PROFILE_DIR_ENV, str(tmp_path / "profiles"))

    report = asyncio.run(diagnostics.diagnose())

    assert report["ok"] is True
    assert report["message"] == "ready"
    assert report["executable_path"] == "/browsers/chrome"
    assert report["versions"]["webskrap"]
    assert report["versions"]["python"]
    assert report["cpu_architecture"] == diagnostics.platform.machine()
    assert report["font_count"] == 17
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
    assert report["chromium_sandbox_one_shot"] is True
    assert report["sessions_root_symlink"] is False


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
        {
            "session": "shop",
            "running": False,
            "pid": None,
            "port": None,
            "chromium_sandbox": None,
            "proxy_server": None,
        }
    ]


def test_package_version_returns_none_for_an_absent_distribution() -> None:
    assert diagnostics.package_version("webskrap-does-not-exist") is None


def test_font_count_deduplicates_primary_families(monkeypatch: Any) -> None:
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _command: "/usr/bin/fc-list")

    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["fc-list"],
            returncode=0,
            stdout="Liberation Sans,Liberation Sans Regular\nLiberation Sans\nNoto Serif\n",
        )

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)

    assert diagnostics.font_count() == 2


def test_font_count_reports_missing_fontconfig(monkeypatch: Any) -> None:
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _command: None)

    assert diagnostics.font_count() is None


@pytest.mark.browser
def test_diagnose_probes_a_real_browser(monkeypatch: Any, sandbox_supported: bool) -> None:
    # The probe launches with the same sandbox setting fetches use, so a host
    # that cannot sandbox needs the documented opt-out to report ready.
    if not sandbox_supported:
        monkeypatch.setenv(browser_session.SANDBOX_ENV, "0")

    report = asyncio.run(diagnostics.diagnose())

    assert report["ok"] is True
    assert report["channel"] in ("chrome", "msedge", "chromium")
    assert Path(str(report["executable_path"])).exists()


@pytest.mark.parametrize("zone", ["UTC", ":Etc/UTC", "Etc/Universal"])
def test_utc_host_timezone_is_a_warning(monkeypatch: Any, zone: str) -> None:
    _stub_probe(monkeypatch)
    monkeypatch.setenv("TZ", zone)

    report = asyncio.run(diagnostics.diagnose())

    assert report["host_timezone"] == zone.lstrip(":")
    assert len(report["warnings"]) == 1
    assert "exit IP" in report["warnings"][0]


def test_local_host_timezone_has_no_warning(monkeypatch: Any) -> None:
    _stub_probe(monkeypatch)
    monkeypatch.setenv("TZ", "Europe/Paris")

    report = asyncio.run(diagnostics.diagnose())

    assert report["host_timezone"] == "Europe/Paris"
    assert report["warnings"] == []


@pytest.mark.parametrize(("env", "expected"), [(None, True), ("0", False)])
def test_doctor_probes_with_the_one_shot_sandbox_setting(
    monkeypatch: Any, env: str | None, expected: bool
) -> None:
    seen: list[bool] = []

    async def fake_doctor(*, chromium_sandbox: bool) -> dict[str, Any]:
        seen.append(chromium_sandbox)
        return {"ok": True, "message": "ready"}

    monkeypatch.setattr(diagnostics, "browser_doctor", fake_doctor)
    if env is None:
        monkeypatch.delenv(browser_session.SANDBOX_ENV, raising=False)
    else:
        monkeypatch.setenv(browser_session.SANDBOX_ENV, env)

    asyncio.run(diagnostics.diagnose())

    assert seen == [expected]
