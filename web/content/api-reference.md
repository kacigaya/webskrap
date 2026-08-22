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

## Persistent browser sessions

Used by `webskrap browser` and the MCP `browser_*` tools, in
`webskrap.browser_session` and `webskrap.paths`.

| Symbol | Description |
| --- | --- |
| `open_session(name, *, headless=True, chromium_sandbox=None)` | Start or reuse a detached Chromium. `chromium_sandbox=None` consults `WEBSKRAP_CHROMIUM_SANDBOX` and otherwise keeps the sandbox; `False` adds `--no-sandbox` and gives up renderer containment. |
| `launch_browser(directory, *, executable, headless, chromium_sandbox=True)` | Launch the browser process for a session directory. Never retries without the sandbox. |
| `sandbox_enabled(chromium_sandbox=None)` | Resolve the sandbox decision: explicit argument, then `WEBSKRAP_CHROMIUM_SANDBOX`, then on. |
| `create_session_dir(name)` | Create a session's directory tree `0700` on POSIX. |
| `resolve_output_path(path, *, root=None, suffix=".png")` | Resolve an untrusted relative destination inside an output root, rejecting absolute paths and anything that escapes it. Raises `WebSkrapError`. |
| `output_root()` | The confinement root for model-supplied output: `./webskrap-output`, or `WEBSKRAP_OUTPUT_DIR`. |
| `secure_directory(path)` | Create (or tighten) a directory to `0700` on POSIX. |

## Profiles

| Symbol | Description |
| --- | --- |
| `BrowserProfile` | Browser-visible settings: viewport, screen, locale, `timezone_id`, `navigator_languages`, headers, and device characteristics. Keeps `Accept-Language` coherent. |
| `Viewport` | A `width` / `height` pair used for viewport and screen dimensions. |
| `get_profile(name)` | Return a copy of a built-in profile (`desktop-chrome`, `desktop-edge`, `mobile-chrome`). |
| `list_profiles()` | List the built-in profiles. |
