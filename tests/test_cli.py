from __future__ import annotations

import json
import subprocess
from typing import Any

from typer.testing import CliRunner

from webskrap import cli
from webskrap.models import FetchResult

runner = CliRunner()


class _FakeClient:
    calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
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
        "text_truncated": True,
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

    assert result.exit_code == 1
    assert "Browser did not launch" in result.output
    assert "webskrap install" in result.output
    assert "Traceback" not in result.output


def test_fetch_does_not_swallow_unrelated_errors(monkeypatch: Any) -> None:
    class _BrokenClient(_FakeClient):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

    monkeypatch.setattr(cli, "WebSkrapClient", _BrokenClient)

    result = runner.invoke(cli.app, ["fetch", "https://example.test"])

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)


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


class _FakeResponse:
    def __init__(self, version: str) -> None:
        self._version = version

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps({"info": {"version": self._version}}).encode()


def test_is_newer_truth_table() -> None:
    assert cli._is_newer("0.6.0", "0.5.6") is True
    assert cli._is_newer("0.5.7", "0.5.6") is True
    assert cli._is_newer("0.5.6", "0.5.6") is False
    assert cli._is_newer("0.5.5", "0.5.6") is False
    assert cli._is_newer("1.0.0rc1", "0.5.6") is False  # malformed -> False


def _enable_update_check(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.delenv("WEBSKRAP_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(cli.sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli, "UPDATE_CHECK_CACHE", tmp_path / "update-check.json")
    monkeypatch.setattr(cli.metadata, "version", lambda _name: "0.5.6")


def test_update_check_opt_out_skips_network(monkeypatch: Any, tmp_path: Any) -> None:
    _enable_update_check(monkeypatch, tmp_path)
    monkeypatch.setenv("WEBSKRAP_NO_UPDATE_CHECK", "1")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("network must not be called")

    monkeypatch.setattr(cli.urllib.request, "urlopen", boom)
    cli._check_for_update()  # no raise = pass


def test_update_check_notifies_and_caches(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    _enable_update_check(monkeypatch, tmp_path)
    calls = {"n": 0}

    def fake_urlopen(*_a: Any, **_k: Any) -> _FakeResponse:
        calls["n"] += 1
        return _FakeResponse("0.9.9")

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)

    cli._check_for_update()
    err = capsys.readouterr().err
    assert "0.9.9 available" in err
    assert "0.5.6" in err
    assert (tmp_path / "update-check.json").exists()

    # Fresh cache -> no second network call.
    cli._check_for_update()
    assert calls["n"] == 1
