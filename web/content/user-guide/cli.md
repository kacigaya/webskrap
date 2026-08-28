---
title: WebSkrap CLI: Scrape Pages from the Terminal
description: Use the WebSkrap command-line interface to fetch pages, return JSON, save HTML, and inspect browser scraping results.
---

# CLI

WebSkrap installs the `webskrap` command.

## Install browsers

```bash
webskrap install
webskrap install --format json
```

`webskrap install` downloads the Chromium browsers used by Playwright and
Patchright. Use JSON output in scripts.

## Check environment

```bash
webskrap doctor
webskrap doctor --format json
```

The CLI `fetch` command uses headless Patchright stealth mode, so `doctor`
checks that Patchright can launch headless Chrome.

## List profiles

```bash
webskrap profiles
webskrap profiles --format json
```

## Fetch a page

```bash
webskrap fetch https://example.com
```

The default human output prints status, final URL, title, and artifact paths.

For LLMs and shell automation, use JSON:

```bash
webskrap fetch https://example.com --format json --max-chars 12000
```

JSON output includes `url`, `final_url`, `status`, `ok`, `title`, `headers`,
`text`, `text_length`, `text_truncated`, and `elapsed_ms`.

## Text and stdout

Print raw fetched content to stdout:

```bash
webskrap fetch https://example.com --stdout
```

Return readable body text instead of HTML:

```bash
webskrap fetch https://example.com --stdout --text-only
webskrap fetch https://example.com --format json --text-only
```

Suppress the human summary when writing artifacts:

```bash
webskrap fetch https://example.com --quiet --output example.html
```

## Screenshot and output

```bash
webskrap fetch https://example.com --screenshot example.png
webskrap fetch https://example.com --output example.html
webskrap fetch https://example.com -o example.html --screenshot example.png
```

Parent directories are created automatically for `--output` and `--screenshot`.

## Wait and timeout

```bash
webskrap fetch https://example.com --wait-until load --timeout-ms 60000
```

Supported wait states are `commit`, `domcontentloaded`, `load`, and
`networkidle`. Prefer `domcontentloaded` for fast HTML collection, `load` for
regular page assets, and `networkidle` only when a page actually needs a quiet
network.

## Resource policy

```bash
webskrap fetch https://example.com --resource-policy lite
webskrap fetch https://example.com --resource-policy documents -o page.html
```

`lite` blocks images, fonts, and media. `documents` also blocks stylesheets.

## Cookie banners

`webskrap fetch` clicks the reject control of a cookie consent notice before
reading the page, so banners do not bury the text and no optional cookies are
accepted. The human output prints `Cookie notice: declined (...)` when it fired,
and the JSON output carries `cookie_notice_declined`.

```bash
webskrap fetch https://example.com --no-decline-cookies
webskrap fetch https://example.com --decline-cookies-timeout-ms 4000
```

The timeout is how long to wait for a late-injected notice, and the per-fetch
cost on pages that have none. Use `0` for a single immediate check.

## Browser channels

`--channel` defaults to `chrome`, which does not exist on every platform (Linux
ARM64 has no Chrome build). When the channel cannot launch, `fetch` prints a
notice on stderr and retries with bundled chromium, so piped stdout stays clean.
If nothing launches you get a one-line error and `Run: webskrap install`, not a
Playwright traceback.

`webskrap doctor` reports the best channel that launches and names it in
`channel`, so `chrome` being unavailable is no longer a failed check.

## Headless Patchright controls

`webskrap fetch` always runs headless Patchright. Use real Chrome and a stable
profile directory when browser continuity matters:

```bash
webskrap fetch https://example.com \
  --channel chrome \
  --user-data-dir .webskrap/headless-profile \
  --mask-headless-user-agent
```

For fingerprint-statistics or WebRTC leak-test pages, apply profile
locale/timezone/media metadata and block non-proxied WebRTC UDP candidates
without viewport, user-agent, or JavaScript patches:

```bash
webskrap fetch https://amiunique.org/fr/fingerprint \
  --channel chrome \
  --mask-headless-user-agent \
  --patchright-context-profile \
  --reduce-fingerprint-surface \
  --webrtc-ip-handling-policy disable_non_proxied_udp
```

Pass additional browser flags with repeated `--launch-arg=...` options. Use the
equals form when the browser flag itself starts with `--`.

```bash
webskrap fetch https://example.com \
  --launch-arg=--no-first-run \
  --launch-arg=--no-default-browser-check
```

## Interactive browser sessions

`webskrap browser` drives a persistent browser with commands modeled on the
official [Playwright CLI](https://playwright.dev/agent-cli/introduction),
implemented natively on Playwright for Python. `open` launches a detached
Chromium that keeps running between commands; every other command reconnects
to it over CDP, acts on the current page, and exits.

```bash
webskrap browser open https://example.com
webskrap browser snapshot
webskrap browser click e15
webskrap browser fill "input[name=q]" "playwright"
webskrap browser press Enter
webskrap browser screenshot page.png --full-page
webskrap browser close
```

`snapshot` prints an aria snapshot of the page where each element carries a
ref like `[ref=e15]`. Interaction commands (`click`, `dblclick`, `hover`,
`fill`, `type`, `select`, `check`, `uncheck`) accept either a ref or any
Playwright selector. Refs describe the current page, so take a fresh
`snapshot` after the page changes.

Navigation uses `goto`, `back`, `forward`, and `reload`; `press` sends a key
to the page, and `eval` runs JavaScript and prints the JSON result. All
commands support `--format json` for scripting and agents, and fail with exit
code 1 and a one-line error instead of a traceback.

Sessions are named with `-s/--session` (default `default`) and listed with
`webskrap browser list`. Each session stores its browser profile under
`~/.webskrap/browser/<name>/` (override the root with `WEBSKRAP_BROWSER_DIR`),
so cookies and logins persist across `open`/`close`. Those directories are
created `0700` on POSIX, because they hold cookies and logged-in state; they
are not encrypted, so use `webskrap browser close --delete-data` to remove a
session's profile, or `close --all` to stop every session. A root you set up
yourself via `WEBSKRAP_BROWSER_DIR` keeps whatever permissions you gave it.

Open and close operations for the same session are serialized across
processes. Concurrent commands cannot launch two browsers against one profile
or leave a stale state file behind.

### Chromium sandbox

`open` keeps Chromium's OS sandbox, which is what contains a renderer
compromised by a hostile page. Some environments cannot start it — containers
without unprivileged user namespaces, or images that disable them — and there
the browser exits during startup with a message saying so.

```bash
webskrap browser open --no-sandbox            # this session only
WEBSKRAP_CHROMIUM_SANDBOX=0 webskrap browser open   # every session on this host
```

`--no-sandbox` wins over the environment variable, and a session started
without the sandbox prints a warning. `webskrap browser list` shows a
`Sandbox` column for running sessions. Prefer fixing the host: sandboxing is
the difference between a compromised renderer and a compromised machine.
WebSkrap never drops the sandbox on its own, and never retries a failed launch
without it.

Deliberate limitations versus the official Playwright CLI: one page per
session (no tab commands), bundled Chromium only, and no commands for network
mocking, tracing, or video. `back` and `forward` always reload the page,
because the back/forward cache is disabled to keep history navigation
deterministic.

The [MCP server](/docs/user-guide/mcp#interactive-browser-tools) exposes the
same sessions to agents as `browser_*` tools.
