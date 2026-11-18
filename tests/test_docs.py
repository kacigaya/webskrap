"""Guard the skill's routing and the user guide's complete tables.

The sdist leaves out the skill folders and web/, so this module runs from a
Git checkout only.
"""

from __future__ import annotations

import asyncio
import json
import re
import tomllib
from pathlib import Path

from webskrap import errors, mcp_server

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "webskrap"
SKILL = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
USER_GUIDE = ROOT / "web" / "content" / "user-guide"


def _table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if line.startswith("|") and not re.match(r"\|\s*-", line):
            rows.append([cell.strip().strip("`") for cell in line.strip("|").split("|")])
    return rows


def _section(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    return text.split(f"\n{heading}\n", 1)[1].split("\n## ", 1)[0]


def test_skill_routes_to_existing_references() -> None:
    for relative_path in (
        "references/python-api.md",
        "references/cli.md",
        "references/mcp.md",
    ):
        assert f"]({relative_path})" in SKILL
        assert (SKILL_DIR / relative_path).is_file()


def test_agent_skill_folders_share_one_copy() -> None:
    for agent in (".claude", ".codex"):
        folder = ROOT / agent / "skills" / "webskrap"
        assert (folder / "SKILL.md").resolve() == (SKILL_DIR / "SKILL.md").resolve()
        assert (folder / "references").resolve() == (ROOT / "references").resolve()


def test_skill_routes_dynamic_contracts_to_their_sources() -> None:
    for source in (
        "src/webskrap/config/models.py",
        "src/webskrap/errors.py",
        "src/webskrap/browser/diagnostics.py",
        "src/webskrap/interfaces/cli.py",
        "src/webskrap/interfaces/browser_cli.py",
        "src/webskrap/interfaces/mcp_server.py",
    ):
        assert f"`{source}`" in SKILL


def test_skill_states_how_search_works_and_what_it_excludes() -> None:
    assert "calls no search API" in SKILL
    assert "does not offer Google" in SKILL
    assert "`src/webskrap/scraping/search.py`" in SKILL


def test_mcp_guide_lists_every_registered_tool() -> None:
    tools = _section(USER_GUIDE / "mcp.md", "## Tools")
    documented = {row[0] for row in _table_rows(tools)} - {"Tool"}
    registered = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert documented == registered


def test_cli_guide_lists_every_exit_status() -> None:
    documented = {
        (int(row[0]), row[1])
        for row in _table_rows((USER_GUIDE / "cli.md").read_text(encoding="utf-8"))
        if row[0].isdigit()
    }
    assert documented == {(status, code.value) for code, status in errors.EXIT_CODES.items()}


def test_mcp_registry_metadata_matches_the_release() -> None:
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert f"<!-- mcp-name: {server['name']} -->" in readme
    (package,) = server["packages"]
    assert package["identifier"] == project["name"]
    assert server["version"] == package["version"] == project["version"]
    assert package["packageArguments"] == [{"type": "positional", "value": "mcp"}]
