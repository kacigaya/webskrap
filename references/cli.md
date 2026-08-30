# CLI

Read this reference when changing or using `webskrap` commands. Treat
`src/webskrap/cli.py` and `src/webskrap/browser_cli.py` as the source of truth
for arguments and help text.

## One-shot commands

`webskrap fetch` uses headless Patchright stealth mode. `webskrap install`
downloads the Playwright and Patchright Chromium browsers. `doctor` reports
readiness, while `schema` describes commands as JSON.

```bash
webskrap doctor --format json
webskrap schema
webskrap profiles --format json
webskrap fetch https://example.com --profile desktop-chrome
webskrap fetch https://example.com --format json --max-chars 4000
webskrap fetch https://example.com --format json --links --max-links 20
webskrap fetch https://example.com --stdout --text-only
webskrap fetch https://example.com --quiet --output page.html
```

Use `--offset` with the previous result's next offset to continue reading a
long page. Do not re-fetch with an ever larger limit.

On Linux ARM64, the `chrome` channel may be unavailable. `fetch` retries a
failed launch with bundled Chromium. Pass `--channel chromium` to avoid the
first attempt.

## Persistent browser

`webskrap browser open` launches detached Chromium. Later commands reconnect
over CDP, act on its current page, and exit.

```bash
webskrap browser open https://example.com
webskrap browser snapshot
webskrap browser click e15
webskrap browser fill "input[name=q]" "playwright"
webskrap browser press Enter
webskrap browser wait --text "Results"
webskrap browser screenshot page.png --full-page
webskrap browser close
```

Snapshot refs become stale when the DOM changes. Take another snapshot before
retrying. `wait` accepts one condition. Do not guess when callers provide more
than one.

Sessions default to the name `default`. Profiles live below
`~/.webskrap/browser/<name>/`, unless `WEBSKRAP_BROWSER_DIR` changes the root.
They contain cookies and logins and must remain mode `0700`.

Keep Chromium's OS sandbox enabled. The explicit opt-outs are
`webskrap browser open --no-sandbox` for one session and
`WEBSKRAP_CHROMIUM_SANDBOX=0` for a host. Document that disabling it leaves a
compromised renderer uncontained.

Current limitations are one page per session, bundled Chromium only, no tabs,
network mocking, tracing, or video, and history navigation that reloads.
