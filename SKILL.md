---
name: webskrap
description: Use when writing, debugging, documenting, or reviewing Python scraping and browser automation code with WebSkrap. Covers async fetches, persistent sessions, Playwright/Patchright drivers, browser profiles, resource policies, screenshots, proxies, timeouts, LLM-friendly CLI output, MCP server usage, and safe examples that avoid CAPTCHA solving, login-wall bypassing, credential bypassing, or access-control circumvention.
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

Install normal API/browser support:

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

Every `fetch()` auto-declines cookie consent notices before reading the page
(`src/webskrap/consent.py`). Turn it off or retune the wait per call:

```python
config = SessionConfig(decline_cookies=False, decline_cookies_timeout_ms=2_000)
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

On Linux ARM64, the `chrome` channel can be unsupported. Prefer `chrome` where
it launches, but use Chromium fallback on Linux ARM64:

```bash
webskrap fetch https://example.com --channel chromium --format json
```

`fetch --format json` prints bounded JSON to stdout using the MCP-compatible
shape: `url`, `final_url`, `status`, `ok`, `title`, `headers`, `text`,
`text_length`, `text_truncated`, `elapsed_ms`, and `cookie_notice_declined`.

Use `--stdout` for raw fetched content, and combine it with `--text-only` for
readable body text.

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
- `doctor`: Playwright/Chromium MCP readiness check.

## Validation

For non-trivial changes run:

```bash
pytest -q
ruff check .
python -m build
```

Use `WEBSKRAP_LIVE=1 pytest -q -m live` only when explicitly checking public
third-party bot-detection behavior. Those tests are opt-in and can fail when
external demos change.
