---
name: webskrap
description: Use when writing, debugging, documenting, or reviewing Python scraping and browser automation code with WebSkrap. Not for ordinary HTTP clients or web search.
---

# WebSkrap

WebSkrap is an async Python scraping package built on Playwright, with
Patchright support for stealth-oriented browser sessions. The Python API,
`webskrap` CLI, and `webskrap-mcp` server share core behavior.

WebSkrap loads known URLs. It does not search the web.

## Workflow

1. Identify the affected public surface: Python, CLI, MCP, or more than one.
2. Read its implementation and nearest tests. Trace calls into shared code
   before editing.
3. Change the shared implementation when behavior belongs to more than one
   surface. Do not patch each wrapper independently.
4. Update affected public contracts: models, serialization, errors, command
   help, MCP schemas, guides, and tests.
5. Run focused tests during development, then the full validation gate before
   handoff when practical.

## Route by task

| Task | Implementation | Tests and contracts |
| --- | --- | --- |
| Fetching, sessions, links, or screenshots | `src/webskrap/client.py`, `src/webskrap/models.py` | `tests/test_client_unit.py`, `tests/test_models.py` |
| Parsing | `src/webskrap/parsing.py` | `tests/test_parsing.py` |
| Errors, hints, or exit statuses | `src/webskrap/errors.py`, `src/webskrap/cli_output.py` | `tests/test_errors.py`, CLI and MCP callers |
| Main CLI commands | `src/webskrap/cli.py` | `tests/test_cli.py`, `tests/test_diagnostics.py` |
| Persistent browser state | `src/webskrap/browser_session.py`, `src/webskrap/paths.py` | `tests/test_browser_session_unit.py`, `tests/test_paths.py` |
| Interactive browser commands | `src/webskrap/browser_cli.py`, `src/webskrap/mcp_server.py` | `tests/test_browser_cli.py`, `tests/test_browser_integration.py`, `tests/test_mcp.py` |
| MCP tools, resources, or annotations | `src/webskrap/mcp_server.py` | `tests/test_mcp.py`, `src/webskrap/guide.md` |
| Profiles or stealth defaults | `src/webskrap/profiles.py`, `src/webskrap/models.py`, `src/webskrap/client.py` | `tests/test_profiles.py`, opt-in bot-detection tests when needed |
| Cookie rejection | `src/webskrap/consent.py` | `tests/test_consent.py`; `tests/test_consent_live.py` only for selector rot |
| Documentation and examples | `README.md`, `src/webskrap/guide.md` | `tests/test_docs.py`, current CLI help, public Python exports |

Search for every constructor, serializer, command, and schema that uses a
changed model or field. Fetch behavior crosses Python, CLI, and MCP. Persistent
browser behavior crosses the browser CLI and MCP.

## Choose the public surface

| Need | Python | CLI | MCP |
| --- | --- | --- | --- |
| Fetch one known URL | `client.fetch()` | `webskrap fetch` | `stealth_fetch` |
| Preserve cookies or storage | `client.session()` | `webskrap browser open` | `browser_open` |
| Click, fill, wait, or run a flow | Playwright page from a session | `webskrap browser` | `browser_interact` and related tools |
| Diagnose installation | Inspect raised error | `webskrap doctor` | `doctor` |

Use a one-shot fetch for one page. Use a persistent browser only when state or
interaction must survive between operations. Do not repeatedly fetch a page to
drive a multi-step flow.

Read only the reference needed for the task:

- [Python API](references/python-api.md): sessions, Patchright, consent, links,
  and fingerprint controls.
- [CLI](references/cli.md): one-shot fetches, persistent browser commands, and
  Linux ARM64 behavior.
- [MCP](references/mcp.md): tool selection, confined paths, annotations, and
  keeping tool results small.

## Guardrails

- Use public pages, local test servers, or targets the user is authorized to
  access.
- Do not add CAPTCHA solving, login-wall bypass, credential bypass, or
  access-control circumvention.
- Do not commit cookies, storage state, proxy credentials, or persistent browser
  data such as `.webskrap/`.
- Preserve Chromium's sandbox by default. Any opt-out must remain explicit and
  outside page-controlled input.
- Keep MCP file paths confined to their configured roots. Reject absolute paths,
  traversal, and symlink escapes.
- Prefer public `webskrap` exports. Use private helpers only in tests.
- Keep changes typed. Add focused tests for parsing, state transitions, output
  contracts, and tool-safety behavior.

## Public contract checklist

When public behavior changes, check all applicable items:

- Python models and exports
- CLI arguments, help, JSON output, and exit status
- MCP input schema, result shape, annotations, and server instructions
- `README.md` and `src/webskrap/guide.md`
- unit tests for each affected surface

Use the implementation as the source of truth. In particular, read
`src/webskrap/models.py` for result fields, `src/webskrap/errors.py` for error
codes and exits, and `src/webskrap/diagnostics.py` for environment reporting.
Do not copy these catalogs into the skill.

## Validation

Run the nearest tests while editing. Before handoff, run the repository gate
when practical:

```bash
uv run --extra dev pytest -q -m "not browser and not live"
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uvx --from pyright==1.1.411 pyright --pythonpath .venv/bin/python src/webskrap
uv run --extra dev bandit -c pyproject.toml -q -r src/
uv run --extra dev --with pip-audit==2.9.0 pip-audit
uv build
```

Run browser tests when browser behavior changes and the required browsers are
installed.

Use `WEBSKRAP_LIVE=1 pytest -q -m live` only when explicitly checking public
third-party behavior. Live tests are non-deterministic and do not replace the
default suite.
