---
title: MCP Web Scraping Server for AI Agents
description: Run WebSkrap as an MCP server so LLM agents can fetch live browser-rendered pages as clean text.
---

# MCP server

WebSkrap ships a [Model Context Protocol](https://modelcontextprotocol.io)
server. MCP clients such as Claude Desktop, Claude Code, and Codex can call it to
drive a real browser directly. It runs over stdio and exposes three tools.

**Built for LLMs.** Both `fetch` and `stealth_fetch` run the same CDP-leak-free
Patchright stealth path the CLI uses (headless Chrome, `networkidle` wait), so
JS-heavy and anti-bot pages that block naive scrapers still load. They return
clean visible page text by default — no HTML tags, scripts, or CSS noise — so
the model spends tokens on content, not markup (typically 5-10x fewer tokens
than raw HTML). Use `stealth_fetch` for finer fingerprint/WebRTC/UA control.
Pass `text_only=false` when you actually need the HTML.

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
| `doctor` | Check that Playwright and Chromium can launch. |

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
| `profile` | `desktop-chrome` | Bundled profile name. |
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
  "user_data_dir": ".webskrap/headless-profile",
  "mask_headless_user_agent": true,
  "patchright_context_profile": true,
  "webrtc_ip_handling_policy": "disable_non_proxied_udp"
}
```
