"""Guards against documentation drifting away from the code it describes.

SKILL.md is what an agent reads before touching this repo, and guide.md is what
an MCP client reads at runtime. A tool, code, or command missing from either is
one nothing will find, so the omission fails here rather than in use.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import typer

from webskrap import cli, diagnostics, mcp_server
from webskrap.errors import EXIT_CODES, ErrorCode

SKILL = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")


def _browser_command_names() -> set[str]:
    command = typer.main.get_command(cli.app)
    browser = command.commands["browser"]  # type: ignore[attr-defined]
    return set(browser.commands)


def _top_level_command_names() -> set[str]:
    return set(typer.main.get_command(cli.app).commands)  # type: ignore[attr-defined]


@pytest.mark.parametrize("code", list(ErrorCode))
def test_skill_documents_every_error_code(code: ErrorCode) -> None:
    assert f"`{code}`" in SKILL


@pytest.mark.parametrize("code", list(ErrorCode))
def test_skill_documents_every_exit_status(code: ErrorCode) -> None:
    row = next(line for line in SKILL.splitlines() if line.startswith(f"| `{code}` |"))

    assert f"| {EXIT_CODES[code]} |" in row


def test_skill_lists_every_cli_command() -> None:
    for name in _top_level_command_names() | _browser_command_names():
        assert f"`{name}`" in SKILL or f"webskrap {name}" in SKILL, name


def test_skill_lists_every_mcp_tool() -> None:
    for tool in asyncio.run(mcp_server.mcp.list_tools()):
        assert f"`{tool.name}`" in SKILL, tool.name


def test_skill_lists_every_mcp_resource() -> None:
    for resource in asyncio.run(mcp_server.mcp.list_resources()):
        assert f"`{resource.uri}`" in SKILL, resource.uri


def test_skill_documents_every_environment_override() -> None:
    for name in diagnostics.ENVIRONMENT_VARIABLES:
        assert f"`{name}`" in SKILL, name


def test_skill_states_that_there_is_no_search() -> None:
    # The one thing an agent will look for and not find; saying so up front is
    # cheaper than a wasted call and a wrong guess about which tool to use.
    assert "There is no search." in SKILL
