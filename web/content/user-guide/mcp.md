---
title: MCP Web Scraping Server for AI Agents
description: Run WebSkrap as an MCP server so LLM agents can fetch live browser-rendered pages as clean text.
---

# MCP server

WebSkrap ships a [Model Context Protocol](https://modelcontextprotocol.io)
server. MCP clients such as Claude Desktop, Claude Code and Codex call it to
drive a real browser. It runs over stdio. Fetch tools handle one-shot scraping,
and `browser_*` tools drive persistent interactive sessions.

`fetch` and `stealth_fetch` run the CLI's stealth path: headless Chrome on
WebSkrap's own CDP client, which never enables the `Runtime` domain. So
JavaScript-heavy pages and anti-bot pages that block naive scrapers still load.
By default they return the visible page text, with no HTML tags, scripts or
CSS, so the model spends tokens on content instead of markup. Pass
`text_only=false` for the HTML. `stealth_fetch` adds finer fingerprint, WebRTC
and user-agent controls.

Both fetch tools default to `domcontentloaded` and `resource_policy="all"`,
because page scripts can see a blocked image or font, which no real browser
blocks. `auto` skips images, fonts and media for text, which is faster on pages
that do not check. The two tools share warm sessions, so they share the
default. An explicit `all`, `lite` or `documents` policy wins. Use `documents`
only when the result survives without CSS. Use `ready_selector` for deferred
content, because earlier load states can read the page before it arrives. Keep
cookie rejection on unless you know the page does not need it. Routing disables
the HTTP cache and does not intercept requests that a service worker owns.

## Install

```bash
pip install webskrap
webskrap install
```

Run the server:

```bash
webskrap-mcp
```

`webskrap mcp` starts the same server, which suits `uvx`, since `uvx` runs a
package's main command:

```bash
uvx webskrap mcp
```

You can also run it as a module:

```bash
python -m webskrap.interfaces.mcp_server
```

## Tools

| Tool | Purpose |
| --- | --- |
| `fetch` | Fetch a URL on the stealth path. |
| `stealth_fetch` | Same stealth path with finer fingerprint/WebRTC/UA controls. |
| `fetch_session_close` | Close an idle warm fetch session; persistent profile files survive. |
| `search` | Find URLs for a query on Bing (default) or DuckDuckGo, with the `stealth_fetch` stealth controls. |
| `doctor` | Check that Chromium can launch, and report fingerprint gaps with native fixes. |
| `browser_open` | Start (or reuse) a persistent browser session with no visible window. |
| `browser_goto` | Navigate the session's current page. |
| `browser_text` | Visible body text, windowed in the browser without an accessibility tree. |
| `browser_find` | Find accessible elements by text or regex with minimal ancestor context. |
| `browser_snapshot` | Aria snapshot of the page with `eN` element refs. |
| `browser_interact` | Click, fill, type, select, hover, check, or uncheck an element. |
| `browser_press` | Press a keyboard key on the page. |
| `browser_solve_captcha` | Click a reCAPTCHA v2 or hCaptcha checkbox inside a form and report `captcha.state`. |
| `browser_wait_for` | Wait for text to appear or disappear, a ref or selector state, or a load state. |
| `browser_view` | Inline viewport PNG and CSS coordinate metadata, without a file write. |
| `browser_mouse` | Coordinate click, double-click, move, scroll, or drag. |
| `browser_insert_text` | Literal Unicode text into the focused control. |
| `browser_watch` | Private live viewer link with optional scroll access. Expires after 15 minutes. |
| `browser_screenshot` | Screenshot the current page to a PNG file. |
| `browser_eval` | Evaluate JavaScript and return the result. |
| `browser_close` | Close a session (`delete_data` removes its profile). |
| `browser_list` | List sessions and whether each is running. |
| `browser_tabs` | List, open, select, or close tabs. Other tools act on the current tab. |

Both fetch tools return `status`, `final_url`, `title`, `ok`, `headers`, and the
page content in `text`. `max_chars` caps `text`, and `text_length` and
`text_truncated` report the full size. `text` holds visible text by default.
Set `text_only` to `false` for raw HTML.

Both also decline cookie banners before they read the page, and report the
strategy that worked in `cookie_notice_declined`. A page with no consent
platform signal adds about 500 ms. A page that loads one can wait up to 2
seconds for its notice. Pass `decline_cookies: false` to leave the notice in
place.

## Tool arguments

`fetch` accepts:

| Argument | Default | Notes |
| --- | --- | --- |
| `url` | required | URL to load. |
| `profile` | `desktop-chrome` | Profile label; the browser's native identity is kept unless `profile_mode` asks for more. |
| `channel` | `chrome` | Browser channel; use `chromium` on Linux ARM64. |
| `wait_until` | `domcontentloaded` | `commit`, `domcontentloaded`, `load`, or `networkidle`. |
| `resource_policy` | `all` | `all`, `auto`, `lite`, or `documents`. |
| `timeout_ms` | `60000` | Timeout applied separately to navigation and readiness. |
| `ready_selector` | `null` | CSS selector or snapshot ref that must become visible after the cookie notice is dismissed. |
| `max_chars` | `8000` | Maximum returned text characters. |
| `text_only` | `true` | Return visible text; set `false` for raw HTML. |
| `decline_cookies` | `true` | Click a cookie consent notice's reject button after load. |
| `solve_cloudflare` | `false` | Click Cloudflare's Turnstile checkbox and wait up to 60 s beyond `timeout_ms` for the real page. Use responsibly. |
| `solve_datadome` | `false` | Drag DataDome's slider CAPTCHA and wait up to 60 s beyond `timeout_ms` for the real page. Use responsibly. |
| `solve_perimeterx` | `false` | Hold HUMAN's (PerimeterX) Press & Hold button and wait up to 60 s beyond `timeout_ms` for the real page. Use responsibly. |
| `solve_recaptcha` | `false` | Click a reCAPTCHA v2 or hCaptcha checkbox on a 403, 429 or 503 page and wait up to 60 s beyond `timeout_ms` for the real page. Use responsibly. |
| `session` | `null` | Opt in to a warm browser with shared cookies/storage. |

Example arguments:

```json
{
  "url": "https://example.com",
  "profile": "desktop-chrome",
  "resource_policy": "lite",
  "wait_until": "load",
  "max_chars": 5000
}
```

`stealth_fetch` takes the same URL, profile, timeout, output-size and session
arguments, `resource_policy` (default `all`) and `wait_until` (default
`domcontentloaded`), plus stealth options:

```json
{
  "url": "https://example.com",
  "channel": "chrome",
  "headless": false,
  "profile_mode": "native",
  "reduce_fingerprint_surface": false,
  "mask_headless_user_agent": false,
  "virtual_display": false,
  "gpu": "auto",
  "webrtc_ip_handling_policy": null
}
```

Both fetch tools and `search` return a `blocked` tool error for known challenge
pages (DataDome, Cloudflare, Akamai, Fastly, AWS WAF) and provider block pages
(Cloudflare, Akamai), even with HTTP 200. For a block, the hint says to change
the exit IP or try `virtual_display=True`. For a challenge, it says to try
`virtual_display=True`, headed mode, or a persistent `user_data_dir`. The
[Stealth guide](/docs/user-guide/stealth/) lists the exact markers.
`browser_goto`, and `browser_open` with a `url`, run the same check but report
the match in `blocked` and keep the page open. Ordinary HTTP error pages return
`ok: false`. With `ready_selector` set, a missing element returns `timeout`, or
`blocked` if a known challenge replaced the page during the wait. Detection
cannot find every challenge or promise that later interactions succeed.

`user_data_dir` must be relative to `~/.webskrap/profiles`. Set
`WEBSKRAP_MCP_PROFILE_DIR` in the server environment to move that root. The
server rejects absolute paths, `..` traversal and symlinks that resolve outside
the root. This applies only to MCP tool input. Python callers can still choose
any `SessionConfig.user_data_dir`.

### Warm fetch sessions

For repeated extraction, pass the same `session` name on each fetch:

```json
{
  "url": "https://example.com",
  "session": "crawl",
  "resource_policy": "lite",
  "decline_cookies": false
}
```

The first call launches the browser. Later calls open new pages in its context,
and cookies and storage are shared within that name. Keep profile and config
options identical, because the server rejects changes. `timeout_ms`,
`decline_cookies`, the four `solve_*` options, output options and `wait_until`
can vary. Concurrent calls can use the same session. Up to eight fetches run at
once, and extra calls wait. Up to eight named sessions stay open, so close
unused ones before you create more.

Call `fetch_session_close(session="crawl")` when done. It returns
`{"closed": true}`, or `false` for an absent session, and refuses a busy one.
The server closes all fetch sessions on shutdown. These sessions are separate
from `browser_*` sessions and do not survive a restart, but persistent
`user_data_dir` files do. Unnamed headless fetches with matching browser
settings can share Chromium while they overlap, each with its own cookies and
storage. Headed fetches, virtual-display fetches, and fetches with an explicit
`user_data_dir` use separate browsers. The last keeps its on-disk state.

`search` takes `query`, `engine` (`bing` by default, or `ddg`), `max_results`,
and the channel, profile, fingerprint and `user_data_dir` arguments of
`stealth_fetch`. It returns `hits` (`title`, `url`, `snippet`) with the
engine's click-tracking unwrapped, plus `hits_total` and `hits_truncated`.
Google is not offered. A `blocked` error means the engine served a bot
challenge. Switch engine or exit IP instead of retrying.

## Interactive browser tools

The `browser_*` tools drive the same persistent sessions as
[`webskrap browser`](/docs/user-guide/cli/#interactive-browser-sessions).
`browser_open` launches a detached browser that keeps running between tool
calls. Every other tool reconnects to it over CDP. The launch uses the same
automation and virtual-screen flags as `stealth_fetch`. Sessions are named
(`session`, default `default`) and keep their profile under
`~/.webskrap/browser/<name>/`, so cookies and logins persist. Set
`WEBSKRAP_BROWSER_DIR` to move the root.

A browser the MCP server launched closes when the server shuts down cleanly,
and its profile stays on disk. A session opened with `webskrap browser open`
keeps running, even when MCP tools use it. To also close the server's own
browsers after a period with no browser tool calls, set
`WEBSKRAP_BROWSER_IDLE_TIMEOUT_S` (seconds) before you start the server. It is
off by default. A server killed by a signal cannot close its browsers. Use
`browser_close` or `webskrap browser close` for those.

`browser_open` accepts `webrtc_ip_handling_policy`, for example
`disable_non_proxied_udp`, to keep the host's addresses out of WebRTC
candidates. A proxy is not a tool argument, and neither is the sandbox switch:
page text can steer a model, and a proxy the model picks could reroute all of a
session's traffic. Open a proxied session with
`webskrap browser open --proxy URL`. The browser tools then reuse it, and
`browser_open` reports its `proxy_server`.

`browser_open` also takes the native stealth settings of `stealth_fetch`:

- `virtual_display`: headed Chromium on a private Xvfb display that lives as
  long as the session (Linux).
- `gpu`: `mesa` renders WebGL through Mesa's lavapipe. `auto` picks it under
  `virtual_display` on a host without a GPU when lavapipe is installed.
- `profile_mode` `locale` with a `profile`: timezone and languages set at
  launch (Linux).

Each tool call reconnects, and CDP emulation ends with its connection, so
`full` mode is not offered. These settings are fixed at launch, so reopening a
running session with a different value is refused.

A typical flow:

1. Call `browser_open` with an optional `url`.
2. Call `browser_snapshot`. Each element carries a ref like `[ref=e15]`.
   Iframe content is listed under its `iframe` line, with refs like
   `f12345e15`.
3. Call `browser_interact` with `action: "click"` and `target: "e15"` (or any
   CSS selector). `fill` and `type` take one entry in `values`. `select` takes
   one or more. `click`, `dblclick` and `type` use human mouse and keystroke
   timing, so they take longer than `fill`. See
   [Humanizer](/docs/user-guide/humanizer/).
4. Read results with `browser_eval` or `browser_screenshot`.
5. Call `browser_close` when done. Pass `delete_data: true` to drop the
   profile.

Snapshots default to `depth=6`. Pass `depth: null` for the full tree. Use
`browser_text` to read without element refs. It takes `max_chars` and `offset`,
and returns `next_text_offset` to page through the current text. Offsets stay
valid only while the text is unchanged. Pass `pattern` (a Python regex of up to
200 characters) to get only matching non-blank lines, each with 0 to 3
`context` lines and at most `max_matches` (1 to 50) results. The regex runs in
Python, never in the page. A pattern that runs longer than 2 seconds fails with
`usage`.

`browser_goto` and `browser_open` accept `decline_cookies: true` to click a
consent notice's reject control and report the strategy in
`cookie_notice_declined`. They wait up to 10 seconds when the page loads a
consent platform, and 500 ms otherwise. They also accept the four `solve_*`
options, which act before the page is reported. For a checkbox inside a form,
`browser_solve_captcha` clicks it and reports `captcha.state`.

`browser_screenshot` accepts `target`, a snapshot ref (iframe refs such as
`f12345e15` too) or CSS selector, to capture only that element's bounding box.
The result reports it as `clip`.

Snapshots are cut at `max_chars` (default 8000) and report
`snapshot_truncated`. Refs describe the current DOM, so take a fresh snapshot
after the page changes. Failed actions return a one-line error.

### Watch a live session

Run `webskrap browser watch visual` for an open session named `visual`, or call
`browser_watch(session="visual")` through MCP. Open the full link in T3 Code
Preview or in a local browser alongside Claude Code or Codex. Open the link
on the same machine, or use a private port forward for a remote session.

The viewer fits the full browser frame into the panel and saves no frames.
Add `--allow-scroll` or set MCP `allow_scroll=true` to let the viewer scroll
with a wheel, trackpad, or keyboard. Focus the preview to use the arrow and
Page Up or Page Down keys. The agent can still scroll the session.
Anyone with the full link and access to the local port can see the viewport. The link expires after 15 minutes.
Press Ctrl+C to stop the CLI viewer early. MCP viewers stop when the server exits.

### Vision and coordinate input

`browser_view(session="visual")` returns an inline `image/png` image block, a
text block with metadata, and the same metadata in `structuredContent`: `url`,
`title`, `width`, `height`, `scroll_x`, `scroll_y`, `mime_type` and
`coordinate_system: "viewport-css-pixels"`. It writes no file and needs an
image-capable model and client. `browser_screenshot` stays the tool for files
and exports.

Open with `browser_open(session="visual", url=...)`, inspect
`browser_view(session="visual")`, then pick coordinates from the target you see
and call `browser_mouse`:

```json
{"action": "click", "x": 120, "y": 80, "session": "visual"}
```

For a focused input, `browser_insert_text(text="hello 世界", session="visual")`
inserts literal text, and `browser_press(key="Enter", session="visual")` sends a
key. Insertion sends one input event, not per-key keydown and keyup events. Use
`browser_interact` with `action="type"` when a control needs real keystrokes.

Mouse actions are `click`, `dblclick`, `move`, `scroll` and `drag`. All require
`x` and `y`. `button` is `left` (default), `middle` or `right`, for click,
double-click and drag only. Drag also requires `end_x` and `end_y`. Scroll takes
`delta_x` and `delta_y` in CSS pixels and targets the element under the
pointer. Positive `delta_y` scrolls down. Invalid, non-finite, unused or
out-of-viewport arguments return a `usage` error before any input is sent.

Use the original PNG size: each image pixel is one CSS pixel, whatever the
device scale. `(0,0)` is the viewport's top-left corner. Map a resized preview
back to `width` and `height`, and do not add scroll offsets. Full-page
screenshots use a different mapping.

Prefer refs and selectors for controls you can identify. A coordinate click hits
whatever is visible at that point. After an action, wait for the expected
change, then take a fresh view before you pick more coordinates. Wheel input can
finish after the call returns. Run dependent actions in sequence in each
session. The image shows the page, including canvas and frames, but not browser
chrome, OS dialogs or a live desktop stream. Headless rendering needs no Xvfb.

### Screenshot output is confined

`browser_screenshot` writes only under `./webskrap-output`, relative to the
server's working directory. Set `WEBSKRAP_OUTPUT_DIR` to move that root. Point
it at a place you accept writes to, not a source tree or `$HOME`.

`path` is a relative destination inside that root. Nested paths work
(`runs/today/page.png`), and missing directories are created. The server
rejects absolute paths, `..` traversal and symlinks that leave the root before
it touches the browser. The model reads untrusted pages, so its file
destinations count as untrusted input.

### Chromium sandbox

MCP sessions keep Chromium's OS sandbox. Hosts that cannot sandbox must set
`WEBSKRAP_CHROMIUM_SANDBOX=0` in the server's environment. It is not a tool
argument, so a page cannot talk the model into weakening renderer isolation.
Session profiles under `~/.webskrap/browser/<name>/` are created `0700` on
POSIX.

The CLI's limits apply: Chromium-based browsers only, and no tools for network
mocking, tracing or video. MCP sessions have no visible window. The CLI also
offers `back`, `forward`, `reload` and `--headed`.

## Register with a client

### Claude Code

```bash
claude mcp add webskrap -- webskrap-mcp
```

### Claude Desktop

```json
{
  "mcpServers": {
    "webskrap": {
      "command": "webskrap-mcp"
    }
  }
}
```

### Codex

Add a server entry to `~/.codex/config.toml`:

```toml
[mcp_servers.webskrap]
command = "webskrap-mcp"
args = []
```

## Stealth

`stealth_fetch` takes the controls from the [Stealth](/docs/user-guide/stealth/)
guide, including `channel`, `headless`, `user_data_dir`, `profile_mode`,
`reduce_fingerprint_surface`, `mask_headless_user_agent`, `virtual_display`,
`gpu` and `webrtc_ip_handling_policy`. On Linux with Xvfb, prefer
`virtual_display` over `mask_headless_user_agent`. It runs Chrome headed on a
private display instead of rewriting the user agent, which empties the
high-entropy client hints.

For best-effort headless stealth from MCP, use real Chrome and turn on only the
native controls you need:

```json
{
  "url": "https://example.com",
  "channel": "chrome",
  "headless": true,
  "user_data_dir": "headless-profile",
  "virtual_display": true,
  "profile_mode": "locale",
  "webrtc_ip_handling_policy": "disable_non_proxied_udp"
}
```

## Targeted snapshots

Use `browser_find(text="Save changes")` to find known controls. Pass exactly one
of `text` (case-insensitive substring) or `regex` (Python regex, with inline
flags such as `(?i)`). Results include matching lines, ancestor paths and refs
for `browser_interact`. `max_matches` takes 1 to 50, default 20. Matches are
counted before character truncation. Your regex runs in a worker with a
two-second limit per frame.

Use `browser_snapshot(target="e15", depth=2)` for one subtree. Unique main-frame
CSS selectors also work. Set `boxes=true` only when you need viewport geometry.
Both tools keep the usual character limit and continuation fields. Repeat the
same query with `next_snapshot_offset` while the DOM is stable.

Snapshots omit password values, hidden and inert content, and background content
behind a native modal dialog. Visible text and other input values can still
hold sensitive data. Open shadow roots and available iframe content come from
the native CDP driver. Closed shadow roots and nested out-of-process frames
keep that driver's limits. Actions do not append snapshots, and no diff cache
is kept.
