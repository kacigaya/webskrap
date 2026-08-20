from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from webskrap import browser_session, cli

runner = CliRunner()

INTERACTIVE_PAGE = (
    "data:text/html,<title>cli-test</title>"
    "<button onclick=\"this.textContent='clicked'\">Press me</button>"
    '<input aria-label="Name">'
    '<select aria-label="Pet"><option>cat</option><option>dog</option></select>'
)


def _env(tmp_path: Path) -> dict[str, str]:
    return {"WEBSKRAP_BROWSER_DIR": str(tmp_path)}


def test_browser_commands_are_registered() -> None:
    result = runner.invoke(cli.app, ["browser", "--help"])

    assert result.exit_code == 0, result.output
    for command in (
        "open",
        "close",
        "list",
        "goto",
        "back",
        "forward",
        "reload",
        "snapshot",
        "click",
        "dblclick",
        "hover",
        "fill",
        "type",
        "select",
        "check",
        "uncheck",
        "press",
        "screenshot",
        "eval",
    ):
        assert command in result.output


def test_target_selector_maps_refs_and_passes_selectors_through() -> None:
    assert browser_session.target_selector("e12") == "aria-ref=e12"
    assert browser_session.target_selector("text=Login") == "text=Login"
    assert browser_session.target_selector("e12x") == "e12x"


def test_invalid_session_name_is_rejected(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["browser", "goto", "https://example.test", "-s", "../evil"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert "invalid session name" in result.output


def test_action_without_open_session_fails(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["browser", "goto", "https://example.test"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "not open" in result.output


def test_corrupt_state_is_treated_as_missing(tmp_path: Path) -> None:
    session_dir = tmp_path / "default"
    session_dir.mkdir()
    (session_dir / "state.json").write_text("not json", encoding="utf-8")

    result = runner.invoke(cli.app, ["browser", "snapshot"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "not open" in result.output


def test_session_running_rejects_recycled_and_dead_pids(tmp_path: Path) -> None:
    session_dir = tmp_path / "default"
    # Alive PID whose command line does not reference this session's user-data
    # directory: must not count as (or ever be killed as) the session browser.
    assert not browser_session.session_running(session_dir, {"pid": os.getpid(), "port": 1})
    assert not browser_session.session_running(session_dir, {"pid": 2**22 - 1, "port": 1})
    assert not browser_session.session_running(session_dir, None)


def test_goto_rejects_invalid_wait_until(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["browser", "goto", "https://example.test", "--wait-until", "sometime"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "commit" in result.output


def test_close_all_and_list_ignore_non_session_directories(tmp_path: Path) -> None:
    (tmp_path / "not a session").mkdir()
    (tmp_path / "backup").mkdir()

    listed = runner.invoke(cli.app, ["browser", "list", "--format", "json"], env=_env(tmp_path))
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output) == {"sessions": []}

    result = runner.invoke(
        cli.app,
        ["browser", "close", "--all", "--delete-data", "--format", "json"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"closed": []}
    assert (tmp_path / "not a session").is_dir()
    assert (tmp_path / "backup").is_dir()


def test_close_unknown_session_fails(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["browser", "close", "-s", "ghost"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "not open" in result.output


def test_list_with_no_sessions(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["browser", "list", "--format", "json"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"sessions": []}


@pytest.mark.browser
def test_browser_session_lifecycle(tmp_path: Path) -> None:
    env = _env(tmp_path)
    opened = runner.invoke(cli.app, ["browser", "open", "--format", "json"], env=env)
    assert opened.exit_code == 0, opened.output
    assert json.loads(opened.output)["reused"] is False

    try:
        reopened = runner.invoke(cli.app, ["browser", "open", "--format", "json"], env=env)
        assert reopened.exit_code == 0, reopened.output
        assert json.loads(reopened.output)["reused"] is True

        goto = runner.invoke(
            cli.app, ["browser", "goto", INTERACTIVE_PAGE, "--format", "json"], env=env
        )
        assert goto.exit_code == 0, goto.output
        assert json.loads(goto.output)["title"] == "cli-test"

        snapshot = runner.invoke(cli.app, ["browser", "snapshot", "--format", "json"], env=env)
        assert snapshot.exit_code == 0, snapshot.output
        tree = json.loads(snapshot.output)["snapshot"]
        assert "[ref=" in tree
        ref = tree.split('button "Press me" [ref=')[1].split("]")[0]

        clicked = runner.invoke(cli.app, ["browser", "click", ref], env=env)
        assert clicked.exit_code == 0, clicked.output

        evaluated = runner.invoke(
            cli.app,
            ["browser", "eval", "document.querySelector('button').textContent", "--format", "json"],
            env=env,
        )
        assert evaluated.exit_code == 0, evaluated.output
        assert json.loads(evaluated.output)["result"] == "clicked"

        filled = runner.invoke(cli.app, ["browser", "fill", "input", "Gaya"], env=env)
        assert filled.exit_code == 0, filled.output
        value = runner.invoke(
            cli.app,
            ["browser", "eval", "document.querySelector('input').value", "--format", "json"],
            env=env,
        )
        assert json.loads(value.output)["result"] == "Gaya"

        selected = runner.invoke(cli.app, ["browser", "select", "select", "dog"], env=env)
        assert selected.exit_code == 0, selected.output
        pet = runner.invoke(
            cli.app,
            ["browser", "eval", "document.querySelector('select').value", "--format", "json"],
            env=env,
        )
        assert json.loads(pet.output)["result"] == "dog"

        # Regression: bfcache restores re-fire no load events; the browser is
        # launched with bfcache disabled so history navigation cannot hang.
        second = runner.invoke(
            cli.app,
            ["browser", "goto", "data:text/html,<title>second</title>", "--format", "json"],
            env=env,
        )
        assert second.exit_code == 0, second.output
        back = runner.invoke(cli.app, ["browser", "back", "--format", "json"], env=env)
        assert back.exit_code == 0, back.output
        assert json.loads(back.output)["title"] == "cli-test"
        forward = runner.invoke(cli.app, ["browser", "forward", "--format", "json"], env=env)
        assert forward.exit_code == 0, forward.output
        assert json.loads(forward.output)["title"] == "second"

        shot_path = tmp_path / "shot.png"
        shot = runner.invoke(cli.app, ["browser", "screenshot", str(shot_path)], env=env)
        assert shot.exit_code == 0, shot.output
        assert shot_path.stat().st_size > 0

        listed = runner.invoke(cli.app, ["browser", "list", "--format", "json"], env=env)
        sessions = json.loads(listed.output)["sessions"]
        assert sessions == [
            {
                "session": "default",
                "running": True,
                "pid": sessions[0]["pid"],
                "port": sessions[0]["port"],
            }
        ]
    finally:
        closed = runner.invoke(cli.app, ["browser", "close", "--format", "json"], env=env)

    assert closed.exit_code == 0, closed.output

    after = runner.invoke(cli.app, ["browser", "snapshot"], env=env)
    assert after.exit_code == 1
    assert "not open" in after.output
