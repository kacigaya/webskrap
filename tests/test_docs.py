"""Guard the skill's routing without duplicating generated API catalogs."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")


def test_skill_routes_to_existing_references() -> None:
    for relative_path in (
        "references/python-api.md",
        "references/cli.md",
        "references/mcp.md",
    ):
        assert f"]({relative_path})" in SKILL
        assert (ROOT / relative_path).is_file()


def test_skill_routes_dynamic_contracts_to_their_sources() -> None:
    for source in (
        "src/webskrap/models.py",
        "src/webskrap/errors.py",
        "src/webskrap/diagnostics.py",
        "src/webskrap/cli.py",
        "src/webskrap/browser_cli.py",
        "src/webskrap/mcp_server.py",
    ):
        assert f"`{source}`" in SKILL


def test_skill_states_that_there_is_no_search() -> None:
    assert "does not search the web" in SKILL
