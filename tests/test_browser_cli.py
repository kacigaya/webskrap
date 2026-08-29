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
        "wait",
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


@pytest.mark.parametrize("session", [".", "..", "../evil"])
def test_invalid_session_name_is_rejected(tmp_path: Path, session: str) -> None:
    result = runner.invoke(
        cli.app,
        ["browser", "goto", "https://example.test", "-s", session],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert "invalid session name" in result.output


@pytest.mark.parametrize("session", [".", ".."])
def test_close_delete_data_rejects_traversal(tmp_path: Path, session: str) -> None:
    browser_root = tmp_path / "browser"
    browser_root.mkdir()
    marker = tmp_path / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        ["browser", "close", "--delete-data", "-s", session],
        env={"WEBSKRAP_BROWSER_DIR": str(browser_root)},
    )

    assert result.exit_code == 1
    assert "invalid session name" in result.output
    assert marker.read_text(encoding="utf-8") == "keep"


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


def test_list_human_output_renders_a_table(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["browser", "list"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "WebSkrap Browser Sessions" in result.output


def test_close_human_output_names_each_session(tmp_path: Path) -> None:
    session_dir = tmp_path / "default"
    session_dir.mkdir()
    (session_dir / "user-data").mkdir()

    result = runner.invoke(cli.app, ["browser", "close", "--all"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "closed" in result.output
    assert "default" in result.output


@pytest.mark.parametrize(
    ("command", "message"),
    [
        pytest.param(["browser", "click", "e1", "extra"], "no value argument", id="click-extra"),
        pytest.param(["browser", "fill", "e1"], "exactly one value", id="fill-missing"),
        pytest.param(["browser", "select", "e1"], "at least one value", id="select-missing"),
    ],
)
def test_element_commands_validate_arity_before_connecting(
    tmp_path: Path, command: list[str], message: str
) -> None:
    result = runner.invoke(cli.app, command, env=_env(tmp_path))

    assert result.exit_code == 1
    assert message in result.output


@pytest.mark.browser
def test_browser_session_lifecycle(persistent_session_env: Path) -> None:
    tmp_path = persistent_session_env
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
                "chromium_sandbox": sessions[0]["chromium_sandbox"],
            }
        ]
    finally:
        closed = runner.invoke(cli.app, ["browser", "close", "--format", "json"], env=env)

    assert closed.exit_code == 0, closed.output

    after = runner.invoke(cli.app, ["browser", "snapshot"], env=env)
    assert after.exit_code == 1
    assert "not open" in after.output


# A page whose content arrives after load, so a wait is the only correct way
# to reach it: polling once is too early and a fixed sleep is a guess.
DEFERRED_PAGE = (
    "data:text/html,<title>deferred</title>"
    "<p id=status>Loading</p>"
    "<script>setTimeout(() => {"
    "document.getElementById('status').textContent = 'Ready';"
    "}, 400)</script>"
)


def test_wait_requires_exactly_one_condition(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["browser", "wait"], env=_env(tmp_path))

    assert result.exit_code != 0
    assert "exactly one of" in result.output


def test_wait_rejects_a_load_state_that_cannot_be_awaited(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app, ["browser", "wait", "--load-state", "commit"], env=_env(tmp_path)
    )

    assert result.exit_code == 2
    assert "domcontentloaded" in "".join(result.output.split())


@pytest.mark.browser
def test_wait_for_deferred_text(persistent_session_env: Path) -> None:
    env = _env(persistent_session_env)
    assert runner.invoke(cli.app, ["browser", "open", DEFERRED_PAGE], env=env).exit_code == 0
    try:
        waited = runner.invoke(
            cli.app,
            ["browser", "wait", "--text", "Ready", "--format", "json"],
            env=env,
        )
        assert waited.exit_code == 0, waited.output
        assert json.loads(waited.output)["matched"] == "text"

        gone = runner.invoke(
            cli.app,
            ["browser", "wait", "--text-gone", "Loading", "--format", "json"],
            env=env,
        )
        assert gone.exit_code == 0, gone.output
        assert json.loads(gone.output)["matched"] == "text_gone"

        by_selector = runner.invoke(
            cli.app,
            ["browser", "wait", "--selector", "#status", "--format", "json"],
            env=env,
        )
        assert by_selector.exit_code == 0, by_selector.output
        assert json.loads(by_selector.output)["matched"] == "selector"

        loaded = runner.invoke(
            cli.app,
            ["browser", "wait", "--load-state", "networkidle", "--format", "json"],
            env=env,
        )
        assert loaded.exit_code == 0, loaded.output
        assert json.loads(loaded.output)["matched"] == "load_state"
    finally:
        runner.invoke(cli.app, ["browser", "close"], env=env)


@pytest.mark.browser
def test_wait_fails_when_the_condition_never_holds(persistent_session_env: Path) -> None:
    env = _env(persistent_session_env)
    assert runner.invoke(cli.app, ["browser", "open", DEFERRED_PAGE], env=env).exit_code == 0
    try:
        result = runner.invoke(
            cli.app,
            ["browser", "wait", "--text", "Never arrives", "--timeout-ms", "500"],
            env=env,
        )

        assert result.exit_code != 0
        assert "Timeout" in result.output
    finally:
        runner.invoke(cli.app, ["browser", "close"], env=env)


@pytest.mark.browser
def test_snapshot_and_eval_are_bounded(persistent_session_env: Path) -> None:
    env = _env(persistent_session_env)
    assert runner.invoke(cli.app, ["browser", "open", INTERACTIVE_PAGE], env=env).exit_code == 0
    try:
        clipped = runner.invoke(
            cli.app,
            ["browser", "snapshot", "--max-chars", "12", "--format", "json"],
            env=env,
        )
        assert clipped.exit_code == 0, clipped.output
        head = json.loads(clipped.output)
        assert len(head["snapshot"]) == 12
        assert head["snapshot_truncated"] is True

        rest = runner.invoke(
            cli.app,
            [
                "browser",
                "snapshot",
                "--offset",
                str(head["next_snapshot_offset"]),
                "--format",
                "json",
            ],
            env=env,
        )
        assert rest.exit_code == 0, rest.output
        assert json.loads(rest.output)["next_snapshot_offset"] is None

        bounded = runner.invoke(
            cli.app,
            ["browser", "eval", "'x'.repeat(500)", "--max-chars", "20", "--format", "json"],
            env=env,
        )
        assert bounded.exit_code == 0, bounded.output
        payload = json.loads(bounded.output)
        assert payload["result"] is None
        assert payload["result_truncated"] is True
        assert payload["result_length"] == 502
    finally:
        runner.invoke(cli.app, ["browser", "close"], env=env)
