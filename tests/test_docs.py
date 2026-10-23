"""Guard the skill's routing without duplicating generated API catalogs."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "webskrap"
SKILL = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


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
        "src/webskrap/models.py",
        "src/webskrap/errors.py",
        "src/webskrap/diagnostics.py",
        "src/webskrap/cli.py",
        "src/webskrap/browser_cli.py",
        "src/webskrap/mcp_server.py",
    ):
        assert f"`{source}`" in SKILL


def test_skill_states_how_search_works_and_what_it_excludes() -> None:
    assert "calls no search API" in SKILL
    assert "does not offer Google" in SKILL
    assert "`src/webskrap/search.py`" in SKILL
