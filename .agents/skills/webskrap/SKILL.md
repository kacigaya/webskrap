---
name: webskrap
description: Use when writing, debugging, documenting, or reviewing Python scraping, web search, and visual browser automation with WebSkrap. Not for ordinary HTTP clients or search APIs.
---

# WebSkrap

WebSkrap is an async Python scraping package that drives Chromium through its
own Chrome DevTools Protocol (CDP) client, with no Playwright. The Python API,
`webskrap` CLI, and `webskrap-mcp` server share core behavior.

WebSkrap loads known URLs. To find URLs it loads DuckDuckGo or Bing's
results page in the same browser and parses the hits. It calls no search API
and it does not offer Google.

## Workflow

1. Identify the affected public surface: Python, CLI, MCP, or more than one.
2. Read its implementation and nearest tests. Trace calls into shared code
   before editing.
3. Change the shared implementation when behavior belongs to more than one
   surface. Do not patch each wrapper independently.
4. Update affected public contracts: Python models and exports, CLI help, JSON
   output and exit status, MCP schemas, result shapes, annotations and server
   instructions, `README.md`, `src/webskrap/interfaces/guide.md`, and unit
   tests for each surface.
5. Run the nearest tests while editing, and browser tests when browser
   behavior changes. Before handoff, run the gate in `CONTRIBUTING.md`; CI runs
   `.github/workflows/ci.yml`. Run `WEBSKRAP_LIVE=1 pytest -q -m live` only to
   check public third-party behavior: live tests are not deterministic.

Use the implementation as the source of truth. Read
`src/webskrap/scraping/results.py` for result fields, `src/webskrap/errors.py` for error
codes and exits, and `src/webskrap/browser/diagnostics.py` for environment reporting.
Do not copy these catalogs into the skill.

## Route by task

| Task | Implementation | Tests and contracts |
| --- | --- | --- |
| Fetching, sessions, links, or screenshots | `src/webskrap/scraping/client.py`, `src/webskrap/scraping/session.py` | `tests/scraping/test_client_unit.py` |
| Search engines, redirect unwrapping, or results parsing | `src/webskrap/scraping/search.py`, `src/webskrap/scraping/results.py` | `tests/scraping/test_search.py` and its fixtures under `tests/fixtures/search/` |
| Parsing | `src/webskrap/config/parsing.py` | `tests/config/test_parsing.py` |
| Errors, hints, or exit statuses | `src/webskrap/errors.py`, `src/webskrap/interfaces/cli_output.py` | `tests/test_errors.py`, CLI and MCP callers |
| Main CLI commands | `src/webskrap/interfaces/cli.py` | `tests/interfaces/test_cli.py`, `tests/browser/test_diagnostics.py` |
| Persistent browser state | `src/webskrap/browser/browser_session.py`, `src/webskrap/security/paths.py` | `tests/browser/test_browser_session_unit.py`, `tests/security/test_paths.py` |
| Interactive browser commands | `src/webskrap/interfaces/browser_cli.py`, `src/webskrap/interfaces/mcp_browser.py` | `tests/interfaces/test_browser_cli.py`, `tests/browser/test_browser_integration.py`, `tests/interfaces/test_mcp.py` |
| Live view (`browser watch`, `browser_watch`) | `src/webskrap/browser/live_view.py` | `tests/browser/test_live_view.py` |
| MCP tools, resources, or annotations | `src/webskrap/interfaces/mcp_server.py` | `tests/interfaces/test_mcp.py`, `src/webskrap/interfaces/guide.md` |
| Profiles or stealth defaults | `src/webskrap/config/profiles.py`, `src/webskrap/config/models.py`, `src/webskrap/scraping/client.py` | `tests/config/test_profiles.py`, opt-in bot-detection tests when needed |
| Cookie rejection | `src/webskrap/browser/consent.py` | `tests/browser/test_consent.py`; `tests/live/test_consent_live.py` only for selector rot |
| Documentation and examples | `README.md`, `src/webskrap/interfaces/guide.md` | `tests/test_docs.py`, current CLI help, public Python exports |

Engine markup lives only in `src/webskrap/scraping/search.py`. When an engine
changes its page, refresh that engine's fixture and fix that one extractor.

## Choose the public surface

| Need | Python | CLI | MCP |
| --- | --- | --- | --- |
| Fetch one known URL | `client.fetch()` | `webskrap fetch` | `stealth_fetch` |
| Find URLs for a query | `client.search()` | `webskrap search` | `search` |
| Preserve cookies or storage | `client.session()` | `webskrap browser open` | `browser_open` |
| See a rendered page or interact with canvas | `browser_session.view()` on a session page | `webskrap browser view` and `mouse` | `browser_view` and `browser_mouse` |
| Let a person watch or drive a session | Not public | `webskrap browser watch` | `browser_watch` |
| Click, fill, wait, or run a flow | `session.new_page()` and `human_click()` | `webskrap browser` | `browser_interact` and related tools |
| Diagnose installation | Inspect raised error | `webskrap doctor` | `doctor` |

Use a one-shot fetch for one page. Use a persistent browser only when state or
interaction must survive between operations. Do not repeatedly fetch a page to
drive a multi-step flow.

Read only the reference needed for the task:

- [Python API](references/python-api.md): sessions, pages, profile modes, consent, links,
  and fingerprint controls.
- [CLI](references/cli.md): one-shot fetches, persistent browser commands,
  vision and live view, and Linux ARM64 behavior.
- [MCP](references/mcp.md): tool selection, confined paths, annotations,
  vision and live view, and keeping tool results small.

## Visual browser interaction

Use `browser_view` (MCP) or `webskrap browser view --format json` (CLI) when
layout, images, canvas, or controls missing from the accessibility tree matter.
MCP returns an image block. CLI saves a PNG: open its `path` with an
image-viewing tool before choosing coordinates. A path alone gives no vision.

One image pixel is one CSS pixel, at any `device_scale_factor`. `(0, 0)` is the
viewport's top-left. Map a resized preview back to `width` and `height`, and do
not add `scroll_x` or `scroll_y`. Full-page screenshots do not map to the mouse.
Prefer refs/selectors for known controls, and take a fresh view after
scrolling, navigation, or layout changes.

To let a person watch or drive a session, use `browser_watch` (MCP) or
`webskrap browser watch` (CLI). It is not a model input. Hand over the full
link: its fragment is the access token. `device_scale_factor=2` sharpens the
view but changes `devicePixelRatio` for every page.

## Guardrails

- Use public pages, local test servers, or targets the user is authorized to
  access.
- Keep CAPTCHA handling in `src/webskrap/challenges/` to the opt-in Cloudflare
  Turnstile checkbox click (`solve_cloudflare`), DataDome slider drag
  (`solve_datadome`), HUMAN Press & Hold (`solve_perimeterx`) and
  reCAPTCHA/hCaptcha checkbox click (`solve_recaptcha`,
  `browser_solve_captcha`). Never touch their image or audio challenges. Do
  not add token injection, solving services, login-wall bypass, credential
  bypass, or access-control circumvention.
- Do not commit cookies, storage state, proxy credentials, or persistent browser
  data such as `.webskrap/`.
- Treat live-view links as credentials: never log, publish, or send them to
  external services. Enable viewer control only when the user asks for it.
- Preserve Chromium's sandbox by default. Any opt-out must remain explicit and
  outside page-controlled input.
- Keep MCP file paths confined to their configured roots. Reject absolute paths,
  traversal, and symlink escapes.
- Prefer public `webskrap` exports. Use private helpers only in tests.
