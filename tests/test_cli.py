from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from webskrap import cli
from webskrap.errors import EXIT_CODES, RECOVERY_HINTS, ErrorCode
from webskrap.models import FetchResult, Link

runner = CliRunner()


class _FakeClient:
    calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        return None

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append({"url": url, **kwargs})
        text = "Readable body" if kwargs.get("text_only") else "<html>abcdef</html>"
        return FetchResult(
            url=url,
            final_url=f"{url}/final",
            status=200,
            ok=True,
            headers={"content-type": "text/html"},
            text=text,
            title="Example",
            cookies=[],
            timings={"elapsed_ms": 12.34},
        )


def _fake_client(monkeypatch: Any) -> None:
    _FakeClient.calls = []
    monkeypatch.setattr(cli, "WebSkrapClient", _FakeClient)


def test_fetch_json_is_bounded_and_uses_headless_stealth(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = runner.invoke(
        cli.app,
        ["fetch", "https://example.test", "--format", "json", "--max-chars", "5"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "url": "https://example.test",
        "final_url": "https://example.test/final",
        "status": 200,
        "ok": True,
        "title": "Example",
        "headers": {"content-type": "text/html"},
        "text": "<html",
        "text_length": 19,
        "text_offset": 0,
        "text_truncated": True,
        "next_text_offset": 5,
        "links": [],
        "links_total": 0,
        "links_truncated": False,
        "elapsed_ms": 12.3,
        "cookie_notice_declined": None,
    }

    config = _FakeClient.calls[0]["config"]
    assert config.driver == "patchright"
    assert config.headless is True
    assert config.channel == "chrome"
    assert config.patchright_focus_control is None
    assert config.decline_cookies is True


def test_fetch_no_decline_cookies_flag(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = runner.invoke(
        cli.app,
        [
            "fetch",
            "https://example.test",
            "--no-decline-cookies",
            "--decline-cookies-timeout-ms",
            "500",
        ],
    )

    assert result.exit_code == 0, result.output
    config = _FakeClient.calls[0]["config"]
    assert config.decline_cookies is False
    assert config.decline_cookies_timeout_ms == 500


class _LaunchFailingClient(_FakeClient):
    """Fails like Playwright does when a browser channel is not installed."""

    fail_channels: tuple[str | None, ...] = ("chrome",)
    attempts: list[str | None] = []

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        channel = kwargs["config"].channel
        self.attempts.append(channel)
        if channel in self.fail_channels:
            raise RuntimeError(
                f"Chromium distribution '{channel}' is not found at /opt/google/chrome\n"
                'Run "playwright install chrome"'
            )
        return await super().fetch(url, **kwargs)


def test_fetch_falls_back_to_chromium_when_channel_is_missing(monkeypatch: Any) -> None:
    _FakeClient.calls = []
    _LaunchFailingClient.attempts = []
    _LaunchFailingClient.fail_channels = ("chrome",)
    monkeypatch.setattr(cli, "WebSkrapClient", _LaunchFailingClient)

    result = runner.invoke(cli.app, ["fetch", "https://example.test", "--stdout"])

    assert result.exit_code == 0, result.output
    # The notice goes to stderr so piped stdout stays clean.
    assert result.output.endswith("<html>abcdef</html>")
    assert "retrying with chromium" in result.output
    assert _LaunchFailingClient.attempts == ["chrome", None]


def test_fetch_reports_launch_failure_without_a_traceback(monkeypatch: Any) -> None:
    _FakeClient.calls = []
    _LaunchFailingClient.fail_channels = ("chrome", None)
    monkeypatch.setattr(cli, "WebSkrapClient", _LaunchFailingClient)

    result = runner.invoke(cli.app, ["fetch", "https://example.test"])

    assert result.exit_code == EXIT_CODES[ErrorCode.BROWSER_LAUNCH]
    assert "Browser did not launch" in result.output
    assert "webskrap install" in result.output
    assert "Traceback" not in result.output


def test_fetch_does_not_misreport_unrelated_errors_as_launch_failures(monkeypatch: Any) -> None:
    class _BrokenClient(_FakeClient):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

    monkeypatch.setattr(cli, "WebSkrapClient", _BrokenClient)

    result = runner.invoke(cli.app, ["fetch", "https://example.test"])

    assert result.exit_code == EXIT_CODES[ErrorCode.NAVIGATION]
    assert "ERR_NAME_NOT_RESOLVED" in result.output
    assert "Browser did not launch" not in result.output


def test_fetch_stdout_prints_raw_content(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = runner.invoke(cli.app, ["fetch", "https://example.test", "--stdout"])

    assert result.exit_code == 0, result.output
    assert result.output == "<html>abcdef</html>"


def test_fetch_stdout_text_only_prints_readable_text(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = runner.invoke(
        cli.app,
        ["fetch", "https://example.test", "--stdout", "--text-only"],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "Readable body"
    assert _FakeClient.calls[0]["text_only"] is True


def test_fetch_text_output_summary_uses_text_label(monkeypatch: Any, tmp_path: Path) -> None:
    _fake_client(monkeypatch)
    output = tmp_path / "page.txt"

    result = runner.invoke(
        cli.app,
        [
            "fetch",
            "https://example.test",
            "--text-only",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"Text: {output}" in result.output
    assert "HTML:" not in result.output


def test_fetch_quiet_suppresses_human_summary(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = runner.invoke(cli.app, ["fetch", "https://example.test", "--quiet"])

    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_profiles_json_is_parseable() -> None:
    result = runner.invoke(cli.app, ["profiles", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [profile["name"] for profile in payload["profiles"]] == [
        "desktop-chrome",
        "desktop-edge",
        "mobile-chrome",
    ]


def test_doctor_json_success(monkeypatch: Any) -> None:
    async def fake_doctor() -> dict[str, object]:
        return {"ok": True, "message": "ready"}

    monkeypatch.setattr(cli, "_doctor", fake_doctor)

    result = runner.invoke(cli.app, ["doctor", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"ok": True, "message": "ready"}


def test_doctor_json_failure(monkeypatch: Any) -> None:
    async def fake_doctor() -> dict[str, object]:
        return {"ok": False, "message": "broken", "hint": "fix it"}

    monkeypatch.setattr(cli, "_doctor", fake_doctor)

    result = runner.invoke(cli.app, ["doctor", "--format", "json"])

    assert result.exit_code == 1
    assert json.loads(result.output) == {"ok": False, "message": "broken", "hint": "fix it"}


def test_install_json_success(monkeypatch: Any) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = runner.invoke(cli.app, ["install", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert [tuple(step["command"]) for step in payload["steps"]] == list(cli.INSTALL_COMMANDS)
    assert calls == list(cli.INSTALL_COMMANDS)


def test_install_json_failure(monkeypatch: Any) -> None:
    def fake_run(command: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return_code = 1 if "patchright" in command else 0
        return subprocess.CompletedProcess(
            command,
            return_code,
            stdout="",
            stderr="missing browser",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = runner.invoke(cli.app, ["install", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["steps"][0]["ok"] is True
    assert payload["steps"][1]["ok"] is False
    assert payload["steps"][1]["message"] == "missing browser"


def test_install_json_handles_missing_executable(monkeypatch: Any) -> None:
    def fake_run(command: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "patchright" in command:
            raise FileNotFoundError("missing patchright")
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = runner.invoke(cli.app, ["install", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["steps"][1]["ok"] is False
    assert payload["steps"][1]["message"] == "missing patchright"


def test_install_human_output(monkeypatch: Any) -> None:
    def fake_run(command: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = runner.invoke(cli.app, ["install"])

    assert result.exit_code == 0, result.output
    assert "OK:" in result.output
    assert "playwright" in result.output
    assert "patchright" in result.output
    assert "install" in result.output
    assert "chromium" in result.output


def test_profiles_human_output_lists_every_profile() -> None:
    result = runner.invoke(cli.app, ["profiles"])

    assert result.exit_code == 0, result.output
    for name in ("desktop-chrome", "desktop-edge", "mobile-chrome"):
        assert name in result.output


def test_doctor_human_success(monkeypatch: Any) -> None:
    async def fake_doctor() -> dict[str, object]:
        return {"ok": True, "message": "Patchright headless chrome is ready."}

    monkeypatch.setattr(cli, "_doctor", fake_doctor)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "ready" in result.output


def test_doctor_human_failure_shows_the_hint(monkeypatch: Any) -> None:
    async def fake_doctor() -> dict[str, object]:
        return {"ok": False, "message": "did not launch", "hint": "Run: webskrap install"}

    monkeypatch.setattr(cli, "_doctor", fake_doctor)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 1
    assert "did not launch" in result.output
    assert "webskrap install" in result.output


def test_install_human_reports_failures(monkeypatch: Any) -> None:
    def fake_run(command: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="download failed")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = runner.invoke(cli.app, ["install"])

    assert result.exit_code == 1
    assert "FAILED:" in result.output
    assert "download failed" in result.output


def _plain(output: str) -> str:
    """Flatten CLI output for substring assertions.

    Typer renders usage errors in a Rich panel: styled, box-drawn, and wrapped
    to the terminal width, which differs between a local run and CI. Strip the
    styling and every space so an assertion tests the message, not the width
    it happened to be rendered at.
    """
    return "".join(re.sub(r"\x1b\[[0-9;]*m", "", output).replace("│", "").split())


@pytest.mark.parametrize(
    ("option", "value", "expected"),
    [
        pytest.param("--format", "yaml", "human, json", id="output-format"),
        pytest.param("--wait-until", "eventually", "commit", id="wait-until"),
        pytest.param("--webrtc-ip-handling-policy", "off", "disable_non_proxied_udp", id="webrtc"),
        pytest.param("--resource-policy", "none", "resource-policy", id="resource-policy"),
    ],
)
def test_fetch_rejects_invalid_option_values(option: str, value: str, expected: str) -> None:
    result = runner.invoke(cli.app, ["fetch", "https://example.test", option, value])

    assert result.exit_code == 2
    assert "".join(expected.split()) in _plain(result.output)


def test_fetch_links_are_opt_in(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = runner.invoke(cli.app, ["fetch", "https://example.test", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert _FakeClient.calls[0]["include_links"] is False
    assert json.loads(result.output)["links"] == []


def test_fetch_links_flag_forwards_the_budget(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = runner.invoke(
        cli.app,
        ["fetch", "https://example.test", "--links", "--max-links", "7", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeClient.calls[0]["include_links"] is True
    assert _FakeClient.calls[0]["max_links"] == 7


def test_fetch_human_summary_counts_links(monkeypatch: Any) -> None:
    class _LinkClient(_FakeClient):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            result = await super().fetch(url, **kwargs)
            return result.model_copy(
                update={"links": [Link(href="https://a.test", text="A")], "links_total": 3}
            )

    monkeypatch.setattr(cli, "WebSkrapClient", _LinkClient)

    result = runner.invoke(cli.app, ["fetch", "https://example.test", "--links"])

    assert result.exit_code == 0, result.output
    assert "1 of 3" in result.output


def test_fetch_json_pages_through_text(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = runner.invoke(
        cli.app,
        ["fetch", "https://example.test", "--format", "json", "--max-chars", "5", "--offset", "5"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["text"] == ">abcd"
    assert payload["text_offset"] == 5
    assert payload["next_text_offset"] == 10


def test_doctor_human_prints_the_surrounding_facts(monkeypatch: Any) -> None:
    async def fake_doctor() -> dict[str, Any]:
        return {
            "ok": True,
            "message": "ready",
            "executable_path": "/browsers/chrome",
            "versions": {"webskrap": "2.0.0", "patchright": None},
            "paths": {"output_root": "/tmp/out"},
            "environment": {"WEBSKRAP_OUTPUT_DIR": "/tmp/out", "WEBSKRAP_BROWSER_DIR": None},
            "sessions": [{"session": "shop", "running": True}],
        }

    monkeypatch.setattr(cli, "_doctor", fake_doctor)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "webskrap 2.0.0" in result.output
    # A driver that is not installed is named rather than silently omitted.
    assert "patchright missing" in result.output
    assert "/browsers/chrome" in result.output
    assert "1 (1 running)" in result.output
    # Only the overrides actually set are shown.
    assert "WEBSKRAP_BROWSER_DIR" not in result.output


def test_doctor_human_still_works_without_the_extra_sections(monkeypatch: Any) -> None:
    async def fake_doctor() -> dict[str, Any]:
        return {"ok": False, "message": "did not launch", "hint": "Run: webskrap install"}

    monkeypatch.setattr(cli, "_doctor", fake_doctor)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 1
    assert "did not launch" in result.output


def test_fetch_json_failure_is_a_parseable_envelope(monkeypatch: Any) -> None:
    class _BrokenClient(_FakeClient):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            raise RuntimeError("Timeout 30000ms exceeded\nCall log:\n  - navigating")

    monkeypatch.setattr(cli, "WebSkrapClient", _BrokenClient)

    result = runner.invoke(cli.app, ["fetch", "https://slow.test", "--format", "json"])

    assert result.exit_code == EXIT_CODES[ErrorCode.TIMEOUT]
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["code"] == "timeout"
    assert payload["error"] == "Timeout 30000ms exceeded"
    assert payload["hint"] == RECOVERY_HINTS[ErrorCode.TIMEOUT]


def test_fetch_human_failure_prints_the_hint_to_stderr(monkeypatch: Any) -> None:
    class _BrokenClient(_FakeClient):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            raise RuntimeError("Timeout 30000ms exceeded")

    monkeypatch.setattr(cli, "WebSkrapClient", _BrokenClient)

    result = runner.invoke(cli.app, ["fetch", "https://slow.test"])

    assert result.exit_code == EXIT_CODES[ErrorCode.TIMEOUT]
    assert "Timeout 30000ms exceeded" in result.output
    assert "browser_wait_for" in result.output


def test_schema_describes_the_whole_command_tree() -> None:
    result = runner.invoke(cli.app, ["schema"])

    assert result.exit_code == 0, result.output
    schema = json.loads(result.output)
    assert schema["name"] == "webskrap"
    top_level = {command["name"] for command in schema["commands"]}
    assert {"fetch", "doctor", "install", "profiles", "browser", "schema"} <= top_level

    browser = next(command for command in schema["commands"] if command["name"] == "browser")
    assert {"open", "close", "snapshot", "wait", "eval"} <= {
        command["name"] for command in browser["commands"]
    }


def test_schema_reports_kinds_defaults_and_choices() -> None:
    schema = json.loads(runner.invoke(cli.app, ["schema"]).output)
    fetch = next(command for command in schema["commands"] if command["name"] == "fetch")
    parameters = {parameter["name"]: parameter for parameter in fetch["parameters"]}

    # Typer's parameter classes do not subclass click's, so a wrong reading
    # here would report the positional URL as an option.
    assert parameters["url"]["kind"] == "argument"
    assert parameters["url"]["required"] is True
    assert parameters["profile"]["kind"] == "option"
    assert parameters["profile"]["default"] == "desktop-chrome"
    assert parameters["resource_policy"]["choices"] == ["all", "lite", "documents"]
    assert parameters["launch_args"]["multiple"] is True


def test_schema_omits_the_help_flag_every_command_has() -> None:
    schema = json.loads(runner.invoke(cli.app, ["schema"]).output)

    for command in schema["commands"]:
        assert "help" not in {parameter["name"] for parameter in command["parameters"]}


def test_schema_defaults_are_json_serializable() -> None:
    # A schema that cannot round-trip is not a schema; enum and Path defaults
    # are the ones that would break it.
    schema = json.loads(runner.invoke(cli.app, ["schema"]).output)

    assert json.loads(json.dumps(schema)) == schema


def test_schema_human_format_lists_commands() -> None:
    result = runner.invoke(cli.app, ["schema", "--format", "human"])

    assert result.exit_code == 0, result.output
    assert "WebSkrap Commands" in result.output
    assert "browser open" in "".join(result.output.split("\n"))
