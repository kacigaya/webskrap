# CLI

Read this reference when changing or using `webskrap` commands. Treat
`src/webskrap/cli.py` and `src/webskrap/browser_cli.py` as the source of truth
for arguments and help text.

## One-shot commands

`webskrap fetch` uses headless Patchright stealth mode. `webskrap install`
downloads the Playwright and Patchright Chromium browsers. `doctor` reports
readiness, browser identity, host CPU architecture, and Fontconfig family count.
It warns when the host timezone is UTC. `fetch` and `search` try Chrome, Edge,
then bundled Chromium when a channel cannot launch. `schema` describes commands
as JSON.

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

`webskrap search` loads a results page through the same stealth path and
prints the organic hits. `--engine` is `bing` (default) or `ddg`.

```bash
webskrap search "example domain"
webskrap search "example domain" --engine bing --max-results 5 --format json
```

A `blocked` failure (exit 11) means the engine served a bot challenge; switch
engine, reuse `--user-data-dir`, or change exit IP instead of retrying.

On Linux ARM64, the `chrome` channel may be unavailable. `fetch` retries a
failed launch with bundled Chromium. Pass `--channel chromium` to avoid the
first attempt.

## Persistent browser

`webskrap browser open` launches detached Chromium. Later commands reconnect
over CDP through Patchright, act on its current page, and exit. The browser
gets the same automation and headless-screen flags as `fetch`; `--headed`
also drops the `HeadlessChrome` user-agent token. `--proxy URL` (no
credentials) routes the session through a proxy and defaults WebRTC to
`disable_non_proxied_udp`; `--webrtc-ip-handling-policy` overrides it. Both are
fixed at launch, and reopening with different values is refused.

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

`click`, `dblclick` and `type` use human mouse and keystroke timing (wheel
scrolling, a curved cursor path, a human button hold, spaced keys) and refuse
a covered target; `fill` sets a value at once.

Snapshot refs become stale when the DOM changes. Take another snapshot before
retrying. `wait` accepts one condition. Do not guess when callers provide more
than one.

Sessions default to the name `default`. Profiles live below
`~/.webskrap/browser/<name>/`, unless `WEBSKRAP_BROWSER_DIR` changes the root.
They contain cookies and logins and must remain mode `0700`.

Keep Chromium's OS sandbox enabled. The explicit opt-outs are
`webskrap browser open --no-sandbox` for one session,
`webskrap fetch --no-sandbox` / `webskrap search --no-sandbox` for one-shot
calls, and `WEBSKRAP_CHROMIUM_SANDBOX=0` for a host. Document that disabling it
leaves a compromised renderer uncontained. Never pass `--no-sandbox` (or its
companions) through `--launch-arg`: those flags are blocked.

Current limitations are one page per session, bundled Chromium only, no tabs,
network mocking, tracing, or video, and history navigation that reloads.

## Vision and coordinate input

```bash
webskrap browser open https://example.com -s visual
webskrap browser view viewport.png -s visual --format json
# Inspect the PNG with your image-viewing tool before choosing coordinates.
webskrap browser mouse click 120 80 -s visual
webskrap browser insert-text "hello 世界" -s visual
webskrap browser press Enter -s visual
webskrap browser mouse scroll 400 300 --delta-y 500 -s visual
webskrap browser view viewport.png -s visual --format json
# Inspect the new view before choosing the drag points.
webskrap browser mouse drag 100 150 --end-x 250 --end-y 180 -s visual
webskrap browser view viewport.png -s visual --format json
webskrap browser close -s visual
```

`view` writes a viewport PNG and returns `path` (absolute), `url`, `title`,
`width`, `height`, `scroll_x`, `scroll_y`, `mime_type`, and `coordinate_system`
(`viewport-css-pixels`). Omitting the path generates a unique PNG in the current
directory. JSON contains metadata, not base64 image data: the agent must open
the file with an image-viewing tool.

One image pixel equals one CSS pixel, even on high-DPI pages. `(0, 0)` is the
viewport's top-left. Map resized previews back to the original dimensions;
do not add document scroll offsets. `screenshot --full-page` remains available
for export, but its coordinates cannot be used directly with `mouse`.

`mouse` accepts `click`, `dblclick`, `move`, `scroll`, or `drag`, with required
`x y`. `--button` is `left` (default), `middle`, or `right` for click/double-click/
drag. Drag requires both `--end-x` and `--end-y`. Scroll uses `--delta-x` and
`--delta-y` over the element under `x y`; positive vertical deltas scroll down,
including in nested scroll areas. Non-finite, out-of-viewport, and unused
arguments fail with `usage`. Coordinate input uses direct browser events;
selector-based `click` and `type` retain their human timing and covered-target
checks.

`insert-text` inserts literal Unicode into the focused control with an input
event. It does not interpret `Enter` as a key or emit per-character keydown/
keyup events. Focus with a click or Tab; use `press` for shortcuts and keys,
or selector-based `type` for controls that require individual keystrokes.

Take a fresh view after navigation, scrolling, or layout changes. Wheel input
may return before scrolling finishes; wait for the expected page condition.
Prefer refs/selectors for known controls, and run dependent session actions
sequentially. This shows the page viewport, not browser chrome or OS dialogs.
