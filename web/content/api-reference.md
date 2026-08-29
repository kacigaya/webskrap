---
title: WebSkrap Python API Reference
description: Reference for WebSkrapClient, WebSkrapSession, FetchResult, SessionConfig, BrowserProfile, resource policies, and proxy configuration.
---

# API Reference

> Full typed signatures and docstrings live in the source. Browse them on [GitHub](https://github.com/kacigaya/webskrap/tree/main/src/webskrap).

WebSkrap's public surface is re-exported from the top-level `webskrap` package.

## Client and sessions

| Symbol | Description |
| --- | --- |
| `WebSkrapClient` | Owns the Playwright lifecycle. Use it as an async context manager and call `fetch()` for one-shot requests or `session()` for persistent contexts. |
| `WebSkrapSession` | A persistent browser context kept open across requests. Exposes `fetch()`, `human_click()`, `decline_cookies()`, and the underlying Playwright `context`. |
| `FetchResult` | Result of a fetch: `url`, `final_url`, `status`, `ok`, `headers`, `text`, `title`, `cookies`, `timings`, `screenshot_path`, and `cookie_notice_declined`. |
| `decline_cookies(page)` | Click the reject control of a cookie consent notice on any Playwright page. Returns the strategy that clicked, or `None`. |

## Configuration

| Symbol | Description |
| --- | --- |
| `SessionConfig` | Per-session or per-call settings: driver, channel, headless, timeouts, `user_data_dir`, `storage_state`, `resource_policy`, `decline_cookies`, `decline_cookies_timeout_ms`, proxy, and stealth options. |
| `ProxyConfig` | Proxy `server` with optional `username` and `password`. |
| `ResourcePolicy` | Request-blocking preset: `ALL`, `LITE`, or `DOCUMENTS`. |
| `WebRtcIPHandlingPolicy` | Allowed WebRTC ICE policy values: `default`, `default_public_and_private_interfaces`, `default_public_interface_only`, `disable_non_proxied_udp`. |

## CLI and MCP internals

`webskrap.browser_session` and `webskrap.paths` implement persistent CLI and
MCP browser sessions, sandbox selection, profile storage, and confined MCP
output. These modules are not re-exported from `webskrap` and are not part of
the stable public Python API. Use the [`webskrap browser` CLI](/docs/user-guide/cli#interactive-browser-sessions)
or the [MCP browser tools](/docs/user-guide/mcp#interactive-browser-tools)
instead of importing their helpers directly.

## Profiles

| Symbol | Description |
| --- | --- |
| `BrowserProfile` | Browser-visible settings: viewport, screen, locale, `timezone_id`, `navigator_languages`, headers, and device characteristics. Keeps `Accept-Language` coherent. |
| `Viewport` | A `width` / `height` pair used for viewport and screen dimensions. |
| `get_profile(name)` | Return a copy of a built-in profile (`desktop-chrome`, `desktop-edge`, `mobile-chrome`). |
| `list_profiles()` | List the built-in profiles. |
