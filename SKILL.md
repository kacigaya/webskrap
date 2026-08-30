---
name: webskrap
description: Use when writing, debugging, documenting, or reviewing Python scraping and browser automation code with WebSkrap. Covers async fetches, persistent sessions, Playwright/Patchright drivers, browser profiles, resource policies, screenshots, proxies, timeouts, LLM-friendly CLI output, MCP server usage. Not for HTTP work that needs no browser, and not a web search tool.
---

# WebSkrap

WebSkrap is an async-first Python scraping package built on Playwright, with
Patchright support for stealth-oriented browser sessions. One codebase behind
three surfaces: the Python API, the `webskrap` CLI, and the `webskrap-mcp` MCP
server.

**There is no search.** WebSkrap loads URLs you already have. Finding them is a
different tool's job, and adding a SERP scraper here would be one brittle
selector away from silently returning nothing.

## Read First

Prefer current repo sources over memory:

- `README.md`: user-facing examples and install notes.
- `src/webskrap/client.py`: `WebSkrapClient`, sessions, fetch flow, screenshots.
- `src/webskrap/models.py`: public Pydantic models, `SessionConfig`, result shape.
- `src/webskrap/errors.py`: `ErrorCode`, recovery hints, exit statuses.
- `src/webskrap/profiles.py`: bundled browser profiles.
- `src/webskrap/consent.py`: cookie-banner auto-decline selectors and strategies.
- `src/webskrap/cli.py`: current `webskrap` command behavior.
- `src/webskrap/cli_output.py`: shared CLI formatting and the failure envelope.
- `src/webskrap/browser_session.py`: persistent browser sessions (detached
  Chromium + CDP reconnect) shared by the CLI and MCP server.
- `src/webskrap/browser_cli.py`: `webskrap browser` interactive commands.
- `src/webskrap/mcp_server.py`: MCP tools, resources, annotations, instructions.
- `src/webskrap/guide.md`: the `webskrap://guide` resource an MCP client reads.
- `src/webskrap/diagnostics.py`: what `doctor` reports.
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

## Which entry point

| Goal | Python | CLI | MCP |
| --- | --- | --- | --- |
| Read one page | `client.fetch(url)` | `webskrap fetch URL` | `stealth_fetch` |
| Read a page and its links | `client.fetch(url, include_links=True)` | `webskrap fetch URL --links` | `stealth_fetch(include_links=true)` |
| Read a long page in pieces | slice `result.text` | `--max-chars` + `--offset` | `max_chars` + `offset` |
| Keep cookies across fetches | `client.session(...)` | `webskrap browser open` | `browser_open` |
| Click, type, log in, multi-step | `session.fetch` + Playwright | `webskrap browser click/fill/...` | `browser_interact` |
| Wait for something to appear | `page.wait_for_*` | `webskrap browser wait` | `browser_wait_for` |
| Find out why it broke | — | `webskrap doctor` | `doctor` |
| List commands machine-readably | — | `webskrap schema` | `webskrap://guide` resource |

`fetch` and `stealth_fetch` drive the same stealth browser. `stealth_fetch` adds
fingerprint, WebRTC, user-agent and persistent-profile control, and is the
default choice over MCP. Neither keeps cookies between calls.

Do not open a browser session to read one page, and do not re-fetch a page
repeatedly to drive one flow.

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
        result = await client.fetch("https://example.com", include_links=True)
    print(result.status, result.final_url, result.title)
    print(result.text[:200])
    for link in result.links:
        print(link.href, link.text)


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

Link collection is opt-in per fetch (`include_links=True`, capped by
`max_links`). Hrefs resolve against the final URL, duplicates and
`javascript:` targets are dropped, and `links_total` reports how many existed
before the cap. Links need JavaScript, so a session with
`java_script_enabled=False` returns none.

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
`webskrap doctor` checks this CLI setup; `webskrap schema` describes every
command as JSON.

```bash
pip install webskrap
webskrap install

webskrap doctor
webskrap doctor --format json
webskrap schema
webskrap profiles --format json
webskrap fetch https://example.com --profile desktop-chrome
webskrap fetch https://example.com --format json --max-chars 4000
webskrap fetch https://example.com --format json --max-chars 4000 --offset 4000
webskrap fetch https://example.com --format json --links --max-links 20
webskrap fetch https://example.com --stdout --text-only
webskrap fetch https://example.com --quiet --output page.html
```

On Linux ARM64, the `chrome` channel can be unsupported. `fetch` detects a
launch failure and retries with bundled chromium automatically; pass the channel
explicitly to skip the failed first attempt:

```bash
webskrap fetch https://example.com --channel chromium --format json
```

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
webskrap browser wait --text "Results"
webskrap browser eval "document.title"
webskrap browser screenshot page.png --full-page
webskrap browser close               # profile persists; --delete-data removes it
```

Commands: `open`, `close`, `list`, `goto`, `back`, `forward`, `reload`,
`snapshot`, `click`, `dblclick`, `hover`, `fill`, `type`, `select`, `check`,
`uncheck`, `press`, `wait`, `screenshot`, `eval`. All accept `-s/--session`
(default `default`) and `--format json`. Session profiles live under
`~/.webskrap/browser/<name>/` (`WEBSKRAP_BROWSER_DIR` overrides the root),
created `0700` because they hold cookies and logins.

`wait` takes exactly one of `--text`, `--text-gone`, `--selector` (with
`--state`) or `--load-state`. Two conditions are rejected rather than guessed at.

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
- `doctor`: readiness plus versions, paths, environment overrides and sessions.
- `browser_open`, `browser_goto`, `browser_snapshot`, `browser_interact`,
  `browser_press`, `browser_wait_for`, `browser_screenshot`, `browser_eval`,
  `browser_close`, `browser_list`: persistent interactive browser sessions,
  sharing state and behavior with `webskrap browser` (headless only over MCP;
  `browser_interact` takes `action` + `target` ref/selector + optional `values`).

MCP resources: `webskrap://guide` (the usage guide, `src/webskrap/guide.md`),
`webskrap://profiles`, `webskrap://sessions`. Reading a resource costs no tool
call, so static facts do not have to be fetched as tool results.

Every tool declares a title and readOnly/destructive/idempotent/openWorld
annotations. Interaction tools are marked destructive because a click or an
Enter submits forms on sites WebSkrap does not own. The server also ships
`instructions`, which a client prepends to its context; keep them under 3k
characters and put the long form in `src/webskrap/guide.md`.

`stealth_fetch.user_data_dir` is relative to `~/.webskrap/profiles`; set
`WEBSKRAP_MCP_PROFILE_DIR` in the server environment to move that root. MCP
tool input cannot select an absolute path or escape the root. Python callers
remain free to choose any `SessionConfig.user_data_dir`.

`browser_screenshot` writes only under `./webskrap-output`
(`WEBSKRAP_OUTPUT_DIR` moves the root): `path` is relative, and absolute
paths, `..` traversal, and symlinks leaving the root are rejected. The sandbox
opt-out is an environment variable, never a tool argument, so a page cannot
argue the model into disabling it.

## What each call returns

| Call | Keys |
| --- | --- |
| `fetch`, `stealth_fetch`, `webskrap fetch --format json` | `url`, `final_url`, `status`, `ok`, `title`, `headers`, `text`, `text_length`, `text_offset`, `text_truncated`, `next_text_offset`, `links`, `links_total`, `links_truncated`, `elapsed_ms`, `cookie_notice_declined` |
| `browser_open` | `session`, `pid`, `port`, `reused`, `chromium_sandbox` |
| `browser_goto` | `status`, `url`, `title` |
| `browser_snapshot` | `url`, `title`, `snapshot`, `snapshot_length`, `snapshot_offset`, `snapshot_truncated`, `next_snapshot_offset` |
| `browser_interact`, `browser_press` | `url`, `title` |
| `browser_wait_for` | `url`, `title`, `matched` |
| `browser_eval` | `result`, `result_length`, `result_truncated`; `result_json` replaces `result` when clipped |
| `browser_screenshot` | `url`, `title`, `path` |
| `browser_close` | `closed` |
| `browser_list`, `webskrap://sessions` | `sessions` |
| any failure | `ok: false`, `error`, `code`, `hint` |

`ok` inside a fetch payload is an HTTP-status judgement (2xx/3xx); a 404 returns
normally with `ok: false` and no `code`. A failure envelope always carries
`code` and `hint`, and over MCP arrives as a tool error rather than a result.

## Keeping results small

| Lever | Effect |
| --- | --- |
| `max_chars` | Defaults to 20000 characters, roughly 5k tokens. Start lower. |
| `offset` / `next_text_offset` | Reads the rest instead of re-fetching with a bigger limit. |
| `resource_policy=lite` | Skips images, fonts and media. |
| `text_only` | Readable text instead of markup. On by default over CLI JSON and MCP. |
| `depth` (snapshot) | A shallow tree of the whole page beats the first characters of a deep one. |
| `include_links` | Off by default; on a link-heavy page they cost more than the text. |
| `browser_eval` | Return the value you want, not `document.body.innerHTML`. |

## When it fails

Every failure carries an `ErrorCode` (`src/webskrap/errors.py`), a fixed hint,
and a CLI exit status.

| Code | Exit | What to do |
| --- | --- | --- |
| `usage` | 2 | Re-read the argument's documented values. |
| `timeout` | 3 | Raise `timeout_ms`, weaken `wait_until`, or wait first. |
| `navigation` | 4 | Check the URL, the scheme, and that the host resolves. |
| `browser_launch` | 5 | `webskrap install`; on Linux ARM64 use `channel=chromium`. |
| `sandbox` | 6 | Enable user namespaces, or accept `WEBSKRAP_CHROMIUM_SANDBOX=0`. |
| `no_session` | 7 | `browser_open` / `webskrap browser open` first. |
| `session_unreachable` | 8 | Close the session, then open it again. |
| `stale_ref` | 9 | Snapshot again; refs belong to one snapshot. |
| `path_rejected` | 10 | Paths are relative to a confined root. |
| `internal` | 1 | Unclassified. Run `doctor`. |

Exit statuses describe raised failures. A command that ran to completion and
reports a negative result -- `doctor` on a host with no browser, `install` on a
failed download -- exits 1 and explains itself in its own payload.

## Environment variables

| Variable | Effect | Default |
| --- | --- | --- |
| `WEBSKRAP_BROWSER_DIR` | Root for persistent browser sessions | `~/.webskrap/browser` |
| `WEBSKRAP_MCP_PROFILE_DIR` | Root for MCP `stealth_fetch` profiles | `~/.webskrap/profiles` |
| `WEBSKRAP_OUTPUT_DIR` | Root MCP screenshots are confined to | `./webskrap-output` |
| `WEBSKRAP_CHROMIUM_SANDBOX` | `0` drops Chromium's OS sandbox | sandboxed |
| `WEBSKRAP_LIVE` | `1` enables the opt-in live tests | off |

`webskrap doctor` prints the resolved values of all of these.

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
