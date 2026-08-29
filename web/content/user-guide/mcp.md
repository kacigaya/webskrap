---
title: MCP Web Scraping Server for AI Agents
description: Run WebSkrap as an MCP server so LLM agents can fetch live browser-rendered pages as clean text.
---

# MCP server

WebSkrap ships a [Model Context Protocol](https://modelcontextprotocol.io)
server. MCP clients such as Claude Desktop, Claude Code, and Codex can call it to
drive a real browser directly. It runs over stdio and exposes fetch tools for
one-shot scraping plus `browser_*` tools for persistent interactive sessions.

Both `fetch` and `stealth_fetch` run the same CDP-leak-free Patchright stealth
path the CLI uses (headless Chrome, `networkidle` wait), so JS-heavy and
anti-bot pages that block naive scrapers still load. They return clean visible
page text by default, with no HTML tags, scripts, or CSS noise, so the model
spends tokens on content instead of markup (typically 5-10x fewer tokens than
raw HTML). Use `stealth_fetch` for finer fingerprint/WebRTC/UA control. Pass
`text_only=false` when you need the HTML.

## Install

```bash
pip install webskrap
webskrap install
```

Run the server:

```bash
webskrap-mcp
```

You can also run it as a module:

```bash
python -m webskrap.mcp_server
```

## Tools

| Tool | Purpose |
| --- | --- |
| `fetch` | Fetch a URL with the Patchright stealth driver (waits for `networkidle`). |
| `stealth_fetch` | Same stealth driver with finer fingerprint/WebRTC/UA controls. |
| `doctor` | Check that Patchright and Chromium can launch. |
| `browser_open` | Start (or reuse) a persistent headless browser session. |
| `browser_goto` | Navigate the session's current page. |
| `browser_snapshot` | Aria snapshot of the page with `eN` element refs. |
| `browser_interact` | Click, fill, type, select, hover, check, or uncheck an element. |
| `browser_press` | Press a keyboard key on the page. |
| `browser_screenshot` | Screenshot the current page to a PNG file. |
| `browser_eval` | Evaluate JavaScript and return the result. |
| `browser_close` | Close a session (`delete_data` removes its profile). |
| `browser_list` | List sessions and whether each is running. |

Both fetch tools return `status`, `final_url`, `title`, `ok`, `headers`, and the
page content in `text` (capped by `max_chars`, with `text_length` and
`text_truncated` reporting the full size). By default `text` is clean visible
text; set `text_only` to `false` to get raw HTML.

Both also auto-decline cookie banners before reading the page, and report which
strategy fired in `cookie_notice_declined`. Pass `decline_cookies: false` to
leave the notice in place.

## Tool arguments

`fetch` accepts:

| Argument | Default | Notes |
| --- | --- | --- |
| `url` | required | URL to load. |
| `profile` | `desktop-chrome` | Profile label; native Patchright defaults remain authoritative. |
| `channel` | `chrome` | Browser channel; use `chromium` on Linux ARM64. |
| `wait_until` | `networkidle` | `commit`, `domcontentloaded`, `load`, or `networkidle`. |
| `resource_policy` | `all` | `all`, `lite`, or `documents`. |
| `timeout_ms` | `60000` | Navigation timeout. |
| `max_chars` | `20000` | Maximum returned text characters. |
| `text_only` | `true` | Return clean visible text; set `false` for raw HTML. |
| `decline_cookies` | `true` | Click a cookie consent notice's reject button after load. |

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

`stealth_fetch` accepts the same URL/profile/timeout/output-size controls plus
Patchright options:

```json
{
  "url": "https://example.com",
  "channel": "chrome",
  "headless": false,
  "patchright_context_profile": false,
  "reduce_fingerprint_surface": false,
  "mask_headless_user_agent": false,
  "webrtc_ip_handling_policy": null
}
```

When `user_data_dir` is set, it must be relative to
`~/.webskrap/profiles`. Set `WEBSKRAP_MCP_PROFILE_DIR` in the MCP server
environment to move that root. Absolute paths, `..` traversal, and symlinks
resolving outside the root are rejected. This confinement applies only to MCP
tool input; Python callers can still choose any `SessionConfig.user_data_dir`.

## Interactive browser tools

The `browser_*` tools drive the same persistent sessions as
[`webskrap browser`](/docs/user-guide/cli#interactive-browser-sessions) in the
CLI: `browser_open` launches a detached headless Chromium that keeps running
between tool calls (and between MCP server restarts), and every other tool
reconnects to it over CDP. Sessions are named (`session`, default `default`)
and store their profile under `~/.webskrap/browser/<name>/`
(root overridable with `WEBSKRAP_BROWSER_DIR`), so cookies and logins persist.

A typical flow:

1. `browser_open` with an optional `url`.
2. `browser_snapshot`, where each element carries a ref like `[ref=e15]`.
3. `browser_interact` with `action: "click"` and `target: "e15"` (or any
   Playwright selector). `fill` and `type` take one entry in `values`;
   `select` takes one or more.
4. `browser_eval` or `browser_screenshot` to read results.
5. `browser_close` when done (`delete_data: true` to drop the profile).

Snapshots are truncated to `max_chars` (default 20000) and report
`snapshot_truncated`; refs describe the current DOM, so take a fresh snapshot
after the page changes. Failed actions return a one-line error.

### Screenshot output is confined

`browser_screenshot` writes only under `./webskrap-output`, relative to the
directory the server runs in. Set `WEBSKRAP_OUTPUT_DIR` to move that root, and
point it somewhere you are willing to have written to — not a source tree or
`$HOME`.

`path` is a relative destination inside that root. Nested paths work
(`runs/today/page.png`, directories created as needed); absolute paths, `..`
traversal, and symlinks leaving the root are rejected before the browser is
touched. The model driving these tools reads untrusted pages, so its file
destinations are treated as untrusted input.

### Chromium sandbox

MCP sessions keep Chromium's OS sandbox. Hosts that cannot sandbox must set
`WEBSKRAP_CHROMIUM_SANDBOX=0` in the server's environment; it is deliberately
not a tool argument, so a page cannot talk the model into weakening renderer
isolation. Session profiles under `~/.webskrap/browser/<name>/` are created
`0700` on POSIX.

Limitations, the same as the CLI: one page per session, bundled Chromium only,
and no tools for tabs, network mocking, tracing, or video. MCP sessions are
always headless. The CLI additionally offers `back`, `forward`, `reload`, and
headed mode.

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

`stealth_fetch` uses the Patchright driver and accepts the same controls as the
[Stealth](/docs/user-guide/stealth) guide, including
`channel`, `headless`, `user_data_dir`, `patchright_context_profile`,
`reduce_fingerprint_surface`, `mask_headless_user_agent`, and
`webrtc_ip_handling_policy`.

For headless best-effort stealth from MCP, use real Chrome and opt in only to the
native browser controls you need:

```json
{
  "url": "https://example.com",
  "channel": "chrome",
  "headless": true,
  "user_data_dir": "headless-profile",
  "mask_headless_user_agent": true,
  "patchright_context_profile": true,
  "webrtc_ip_handling_policy": "disable_non_proxied_udp"
}
```
