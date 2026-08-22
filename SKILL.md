---
name: webskrap
description: Use when writing, debugging, documenting, or reviewing Python scraping and browser automation code with WebSkrap. Covers async fetches, persistent sessions, Playwright/Patchright drivers, browser profiles, resource policies, screenshots, proxies, timeouts, LLM-friendly CLI output, MCP server usage.
---

# WebSkrap

WebSkrap is an async-first Python scraping package built on Playwright, with
Patchright support for stealth-oriented browser sessions.

## Read First

Prefer current repo sources over memory:

- `README.md`: user-facing examples and install notes.
- `src/webskrap/client.py`: `WebSkrapClient`, sessions, fetch flow, screenshots.
- `src/webskrap/models.py`: public Pydantic models, `SessionConfig`, result shape.
- `src/webskrap/profiles.py`: bundled browser profiles.
- `src/webskrap/consent.py`: cookie-banner auto-decline selectors and strategies.
- `src/webskrap/cli.py`: current `webskrap` command behavior.
- `src/webskrap/browser_session.py`: persistent browser sessions (detached
  Chromium + CDP reconnect) shared by the CLI and MCP server.
- `src/webskrap/browser_cli.py`: `webskrap browser` interactive commands.
- `src/webskrap/mcp_server.py`: MCP tools and argument shape.
- `tests/`: behavior contracts when changing parsing, state, CLI, stealth, or safety.

## Guardrails

- Do not add CAPTCHA solving, login-wall bypassing, credential bypassing, or
  access-control circumvention.
- Use public pages, local test servers, or targets the user is allowed to access.
- Do not commit cookies, storage state, proxy credentials, or persistent browser
  data such as `.webskrap/`.
- Prefer public exports from `webskrap`; use private helpers only in tests.
- Keep changes small and typed. Add focused tests for parsing, state transitions,
  CLI output, and tool-safety behavior.

## Python API

Install the package and its browsers:

```bash
pip install webskrap
webskrap install
```

Use `WebSkrapClient` as an async context manager. Use `client.fetch()` for a
one-shot fetch. Use `client.session()` when cookies, storage, manual page work,
human-like clicks, or headed debugging should persist.

```python
import asyncio

from webskrap import WebSkrapClient


async def main() -> None:
    async with WebSkrapClient() as client:
        result = await client.fetch("https://example.com")
    print(result.status, result.final_url, result.title)
    print(result.text[:200])


asyncio.run(main())
```

The Python API defaults to Playwright. `pip install webskrap` includes Patchright
and MCP dependencies. For Patchright, opt in through `SessionConfig`:

```python
from pathlib import Path

from webskrap import SessionConfig

config = SessionConfig(
    driver="patchright",
    channel="chrome",
    headless=True,
    user_data_dir=Path(".webskrap/headless-profile"),
)
```

Cookie rejection is opt-in in the Python API (`src/webskrap/consent.py`).
The CLI and MCP server enable it by default:

```python
config = SessionConfig(decline_cookies=True, decline_cookies_timeout_ms=2_000)
```

`FetchResult.cookie_notice_declined` reports the strategy that clicked (`"cmp"`,
`"text"`, or `None`). `session.decline_cookies(page)` does the same on a page you
drive yourself.

Headed Patchright is strongest for strict detection surfaces:

```python
config = SessionConfig(driver="patchright", channel="chrome", headless=False)
```

For fingerprint-statistics or WebRTC leak-test pages, prefer native browser
controls over JavaScript patches:

```python
config = SessionConfig(
    driver="patchright",
    channel="chrome",
    headless=True,
    patchright_context_profile=True,
    reduce_fingerprint_surface=True,
    mask_headless_user_agent=True,
    webrtc_ip_handling_policy="disable_non_proxied_udp",
)
```

## CLI

The CLI `fetch` command always uses headless Patchright stealth mode.
`webskrap install` downloads Playwright and Patchright Chromium browsers;
`webskrap doctor` checks this CLI setup.

```bash
pip install webskrap
webskrap install

webskrap doctor
webskrap doctor --format json
webskrap profiles
webskrap profiles --format json
webskrap fetch https://example.com --profile desktop-chrome
webskrap fetch https://example.com --format json --max-chars 12000
webskrap fetch https://example.com --stdout --text-only
webskrap fetch https://example.com --quiet --output page.html
```

On Linux ARM64, the `chrome` channel can be unsupported. `fetch` detects a
launch failure and retries with bundled chromium automatically; pass the channel
explicitly to skip the failed first attempt:

```bash
webskrap fetch https://example.com --channel chromium --format json
```

`fetch --format json` prints bounded JSON to stdout using the MCP-compatible
shape: `url`, `final_url`, `status`, `ok`, `title`, `headers`, `text`,
`text_length`, `text_truncated`, `elapsed_ms`, and `cookie_notice_declined`.

Use `--stdout` for raw fetched content, and combine it with `--text-only` for
readable body text.

## Interactive browser CLI

`webskrap browser` drives a persistent browser with commands modeled on the
official Playwright CLI, written natively on Playwright for Python. `open`
launches a detached Chromium that keeps running between commands; every other
command reconnects over CDP, acts on the current page, and exits.

```bash
webskrap browser open https://example.com
webskrap browser snapshot            # aria snapshot with [ref=eN] element refs
webskrap browser click e15           # refs or any Playwright selector
webskrap browser fill "input[name=q]" "playwright"
webskrap browser press Enter
webskrap browser eval "document.title"
webskrap browser screenshot page.png --full-page
webskrap browser close               # profile persists; --delete-data removes it
```

Commands: `open`, `close`, `list`, `goto`, `back`, `forward`, `reload`,
`snapshot`, `click`, `dblclick`, `hover`, `fill`, `type`, `select`, `check`,
`uncheck`, `press`, `screenshot`, `eval`. All accept `-s/--session` (default
`default`) and `--format json`; failures exit 1 with a one-line error. Session
profiles live under `~/.webskrap/browser/<name>/` (`WEBSKRAP_BROWSER_DIR`
overrides the root), created `0700` because they hold cookies and logins.
Sessions keep Chromium's OS sandbox; where it cannot start, opt out per
session with `webskrap browser open --no-sandbox` or per host with
`WEBSKRAP_CHROMIUM_SANDBOX=0`, accepting that a compromised renderer is then
uncontained. Refs go stale when the DOM changes; snapshot again.
Limitations: one page per session, bundled Chromium only, and no commands for
tabs, network mocking, tracing, or video. History navigation always reloads.

## MCP

Install MCP support when an MCP client should call WebSkrap directly:

```bash
pip install webskrap
webskrap install
webskrap-mcp
```

MCP tools:

- `fetch`: Patchright stealth fetch (headless Chrome, waits for networkidle).
- `stealth_fetch`: stealth fetch with finer fingerprint/WebRTC/UA controls.
- `doctor`: Patchright/Chromium MCP readiness check.
- `browser_open`, `browser_goto`, `browser_snapshot`, `browser_interact`,
  `browser_press`, `browser_screenshot`, `browser_eval`, `browser_close`,
  `browser_list`: persistent interactive browser sessions, sharing state and
  behavior with `webskrap browser` (headless only over MCP; `browser_interact`
  takes `action` + `target` ref/selector + optional `values`).

`browser_screenshot` writes only under `./webskrap-output`
(`WEBSKRAP_OUTPUT_DIR` moves the root): `path` is relative, and absolute
paths, `..` traversal, and symlinks leaving the root are rejected. The sandbox
opt-out is an environment variable, never a tool argument, so a page cannot
argue the model into disabling it.

## Validation

For non-trivial changes run:

```bash
pytest -q
ruff check .
ruff format --check .
python -m build
```

CI (`.github/workflows/ci.yml`) runs the same gate on push and pull request, and
`Publish` calls it before uploading to PyPI. PyPI versions are immutable, so a
red gate must never be bypassed.

Use `WEBSKRAP_LIVE=1 pytest -q -m live` only when explicitly checking public
third-party bot-detection behavior, or `tests/test_consent_live.py` for CMP
selector rot. Those tests are opt-in and can fail when external sites change.
