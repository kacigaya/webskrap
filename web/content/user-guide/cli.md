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

`webskrap install` downloads Chrome for Testing into the user cache folder.
`--force` downloads it again. Use JSON output in scripts.

## Check environment

```bash
webskrap doctor
webskrap doctor --format json
```

CLI `fetch` runs headless Chrome, so `doctor` checks that headless Chrome can
launch. It also reports the host timezone and warns when it is UTC, which pages
read as a server.

The check launches with a default `fetch`'s flags. `fingerprint` reports what
pages read from that browser: `webdriver`, the user agent and platform, the
client-hint architecture, the WebGL renderer and the media device count.
WebSkrap reads these values from a blank https page it serves itself, so no
request leaves the browser. Each gap gets a warning that names a native fix,
such as `virtual_display=True`, `gpu='mesa'`, `fake_media_devices=True`, or a
host change. No warning suggests a JavaScript patch.

## List profiles

```bash
webskrap profiles
webskrap profiles --format json
```

## Fetch a page

```bash
webskrap fetch https://example.com
```

The default output prints status, final URL, title and artifact paths. For LLMs
and shell scripts, use JSON:

```bash
webskrap fetch https://example.com --format json --max-chars 12000
```

JSON output includes `url`, `final_url`, `status`, `ok`, `title`, `headers`,
`text`, `text_length`, `text_truncated` and `elapsed_ms`.

Wait for deferred content with a visible CSS selector or snapshot ref:

```bash
webskrap fetch https://example.com --ready-selector '#content' --format json
```

`--timeout-ms` applies separately to navigation and to this wait, which starts
after the cookie notice is dismissed. A missing selector exits 3 (`timeout`).
Known challenge and block pages exit 11 (`blocked`) without a retry, even with
HTTP 200: DataDome, Cloudflare, HUMAN, Akamai, Fastly, AWS WAF, and reCAPTCHA
or hCaptcha on a 403, 429 or 503 page. Ordinary HTTP errors return `ok: false`.

## Search the web

```bash
webskrap search "example domain"
webskrap search "example domain" --engine bing --max-results 5 --format json
```

`search` loads Bing's results page (`--engine bing`, the default) or
DuckDuckGo's HTML page (`--engine ddg`) in the same stealth browser. It prints
each hit's title, URL and snippet. WebSkrap unwraps the engine's click-tracking
redirect, so the URL is the destination. Google is not offered. JSON output
carries `query`, `engine`, `hits`, `hits_total` and `hits_truncated`.

A `blocked` failure (exit 11) means the engine served a bot challenge instead of
results. Switch engine, reuse `--user-data-dir`, or change the exit IP. Nothing
is retried.

## Text and stdout

Print the fetched content to stdout:

```bash
webskrap fetch https://example.com --stdout
```

Return readable body text instead of HTML:

```bash
webskrap fetch https://example.com --stdout --text-only
webskrap fetch https://example.com --format json --text-only
```

Hide the summary when you write artifacts:

```bash
webskrap fetch https://example.com --quiet --output example.html
```

## Screenshot and output

```bash
webskrap fetch https://example.com --screenshot example.png
webskrap fetch https://example.com --output example.html
webskrap fetch https://example.com -o example.html --screenshot example.png
```

`--output` and `--screenshot` create missing parent directories.

## Wait and timeout

```bash
webskrap fetch https://example.com --wait-until load --timeout-ms 60000
```

The wait states are `commit`, `domcontentloaded`, `load` and `networkidle`. Use
`domcontentloaded` for fast HTML, `load` for regular page assets, and
`networkidle` only when a page needs a quiet network.

## Resource policy

```bash
webskrap fetch https://example.com --resource-policy lite
webskrap fetch https://example.com --resource-policy documents -o page.html
```

The fetch default is `auto`. With `--text-only` and no screenshot, it skips
images, fonts and media. HTML and screenshots load all resources. An explicit
policy wins. `lite` blocks images, fonts and media. `documents` also blocks
stylesheets. Use it only when the result survives without CSS.

## Cookie banners

`webskrap fetch` clicks the reject control of a cookie consent notice before it
reads the page, so banners do not bury the text and no optional cookies are
accepted. When this happens, the output prints `Cookie notice: declined (...)`
and JSON carries `cookie_notice_declined`.

```bash
webskrap fetch https://example.com --no-decline-cookies
webskrap fetch https://example.com --decline-cookies-timeout-ms 4000
```

The timeout is how long to wait for a late notice, and what each fetch costs on
pages with none. Use `0` for one immediate check.

## Challenge solvers

Four opt-in flags answer one widget each and wait up to 60 seconds for the real
page. All are off by default. Use them responsibly.

| Flag | Action | Details |
| --- | --- | --- |
| `--solve-cloudflare` | Clicks Cloudflare's Turnstile checkbox. | [Stealth](stealth.md#cloudflare-turnstile) |
| `--solve-datadome` | Drags DataDome's slider. | [Stealth](stealth.md#datadome-slider) |
| `--solve-perimeterx` | Holds HUMAN's Press & Hold button. | [Stealth](stealth.md#human-press--hold) |
| `--solve-recaptcha` | Clicks a reCAPTCHA v2 or hCaptcha checkbox on a blocking page. | [Stealth](stealth.md#recaptcha-and-hcaptcha-checkbox) |

```bash
webskrap fetch https://example.com --virtual-display --solve-cloudflare
```

## Browser channels

`--channel` defaults to `chrome`, which some platforms lack (Linux ARM64 has no
Chrome build). When the channel cannot launch, `fetch` and `search` print a
notice on stderr, so piped stdout stays clean. Then they try installed Edge,
then Chrome for Testing (the full browser in new headless mode). A sandbox
failure stops without trying another channel. If nothing launches, you get a
one-line error and `Run: webskrap install`, not a traceback.

`webskrap doctor` follows the same Chrome, Edge, Chromium order. Its output
names the working `channel` and `browser_identity`, the host
`cpu_architecture`, and `font_count`, the number of distinct Fontconfig
families. `font_count` is `null` where `fc-list` is missing.

## Headless stealth controls

`webskrap fetch` always runs headless. When browser continuity matters, use
real Chrome and a stable profile directory:

```bash
webskrap fetch https://example.com \
  --channel chrome \
  --user-data-dir .webskrap/headless-profile \
  --virtual-display
```

`--virtual-display` runs Chrome headed on a private Xvfb display (Linux, with
`Xvfb` installed), so no `HeadlessChrome` token or empty client hints reach the
site. `--mask-headless-user-agent` rewrites the token in headless mode instead,
but Chromium then empties the high-entropy client hints. `--gpu mesa` renders
WebGL with Mesa's lavapipe instead of SwiftShader (Linux,
`mesa-vulkan-drivers`). With `--virtual-display` on a host without a GPU, the
default `--gpu auto` already uses Mesa when lavapipe is installed. See
[WebGL renderer](stealth.md#webgl-renderer).

For fingerprint-statistics or WebRTC leak-test pages, apply the profile's
locale, timezone and media metadata and block non-proxied WebRTC UDP
candidates, with no viewport, user-agent or JavaScript patches:

```bash
webskrap fetch https://amiunique.org/fr/fingerprint \
  --channel chrome \
  --virtual-display \
  --profile-mode locale \
  --reduce-fingerprint-surface \
  --webrtc-ip-handling-policy disable_non_proxied_udp
```

Pass more browser flags with repeated `--launch-arg=...` options. Use the
equals form when the flag itself starts with `--`.

```bash
webskrap fetch https://example.com \
  --launch-arg=--no-first-run \
  --launch-arg=--no-default-browser-check
```

## Interactive browser sessions

`webskrap browser` drives a persistent browser. Its commands follow the
official [Playwright CLI](https://playwright.dev/agent-cli/introduction), built
on WebSkrap's own CDP client. `open` launches a detached browser that keeps
running between commands. Every other command reconnects to it over CDP, acts
on the current page and exits. It uses installed Chrome, then Edge, then Chrome
for Testing. Installed Chrome and Edge include the platform's sandbox support,
such as the AppArmor profile Ubuntu 23.10+ needs.

Sessions use the same stealth path as `webskrap fetch`. Commands never enable
the CDP `Runtime` domain, so no `Runtime.enable` leak reaches the page or its
workers. The browser launches with
`--disable-blink-features=AutomationControlled` (`navigator.webdriver` stays
false) and the 1920x1080 virtual screen of headless fetches. Headless sessions
still report `HeadlessChrome` in the user agent. When a site checks it, pass
`--virtual-display` (headed on a private Xvfb display that lives as long as the
session, Linux) or `--headed`. `--gpu mesa` and `--profile-mode locale
--profile NAME` work as for `fetch`. Each command reconnects, and CDP emulation
ends with its connection, so `--profile-mode full` is not offered.
`browser eval` runs in the page's own JavaScript world, so page globals are
visible.

```bash
webskrap browser open https://example.com
webskrap browser snapshot
webskrap browser click e15
webskrap browser fill "input[name=q]" "chromium"
webskrap browser press Enter
webskrap browser screenshot page.png --full-page
webskrap browser screenshot button.png --target e15   # one element
webskrap browser close
```

`snapshot` prints an aria snapshot where each element carries a ref like
`[ref=e15]`. Interaction commands (`click`, `dblclick`, `hover`, `fill`, `type`,
`select`, `check`, `uncheck`) take a ref or a CSS selector. Refs describe the
current page, so take a fresh `snapshot` after the page changes. Iframe content
is listed under its `iframe` line, and refs in a frame look like `f12345e15`.
CSS selectors match in the main frame only.

`click` and `dblclick` scroll with the mouse wheel, move the cursor along a
curved path, and hold the button for 60-140 ms. `type` clicks into the field and
types with human keystroke timing. Both refuse a target covered by another
element. They are slower than mechanical input on purpose (see
[Humanizer](/docs/user-guide/humanizer/)). `fill` still sets a value at once.

Navigate with `goto`, `back`, `forward` and `reload`. `press` sends a key to the
page, and `eval` runs JavaScript and prints the JSON result. All commands take
`--format json` and fail with a one-line error instead of a traceback. The exit
status names the kind of failure, and JSON carries the same name in `code`:

| Exit | Code                  |
| ---- | --------------------- |
| 1    | `internal`            |
| 2    | `usage`               |
| 3    | `timeout`             |
| 4    | `navigation`          |
| 5    | `browser_launch`      |
| 6    | `sandbox`             |
| 7    | `no_session`          |
| 8    | `session_unreachable` |
| 9    | `stale_ref`           |
| 10   | `path_rejected`       |
| 11   | `blocked`             |

`open URL` and `goto` take `--decline-cookies` and the four `--solve-*` flags.
For a checkbox inside a form, `webskrap browser solve-captcha` clicks it and
prints its state. Both commands also run the fetch challenge and block check,
but report a match instead of failing: the output prints a `blocked:` line and
JSON carries it in `blocked`. The page stays open.

Name sessions with `-s/--session` (default `default`) and list them with
`webskrap browser list`. Each session keeps its browser profile under
`~/.webskrap/browser/<name>/`, so cookies and logins persist across `open` and
`close`. Set `WEBSKRAP_BROWSER_DIR` to change the root. On POSIX the
directories are created `0700` because they hold cookies and logged-in state.
They are not encrypted. Remove a session's profile with
`webskrap browser close --delete-data`, or stop every session with
`close --all`. A root you create yourself through `WEBSKRAP_BROWSER_DIR` keeps
its own permissions.

Open and close for one session are serialized across processes, so concurrent
commands cannot launch two browsers on one profile or leave a stale state file.

### Watch a live session

Run `webskrap browser watch visual` for an open session named `visual`, or call
`browser_watch(session="visual")` through MCP. Open the full link in T3 Code
Preview or in a local browser alongside Claude Code or Codex. Open the link
on the same machine, or use a private port forward for a remote session.

The viewer fits the full browser frame into the panel and saves no frames.
It streams the page as it changes and shows a lossless image once the page is still.
For a sharper view on a high-density screen, open the session with
`--device-scale-factor 2` or MCP `device_scale_factor=2`. Pages then see
`devicePixelRatio` 2, which changes the browser fingerprint. Rendering four
times the pixels lowers the frame rate on a CPU-only host.
Add `--allow-scroll` or set MCP `allow_scroll=true` to let the viewer scroll
with a wheel, trackpad, or keyboard. Focus the preview to use the arrow and
Page Up or Page Down keys. Add `--allow-control` or set MCP
`allow_control=true` to also click and type. Click inside the frame to focus it.
Press Escape to leave viewer focus. Share control links only with people you
trust to act in that browser session.
Anyone with the full link and access to the local port can see the viewport. The link expires after 15 minutes.
Press Ctrl+C to stop the CLI viewer early. MCP viewers stop when the server exits.

### Vision and coordinate input

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
`width`, `height`, `scroll_x`, `scroll_y`, `mime_type` and `coordinate_system`
(`viewport-css-pixels`). Without a path, it writes a uniquely named PNG in the
current directory. JSON holds metadata, not base64 image data, so the agent
must open the file with an image-viewing tool.

One image pixel equals one CSS pixel, even on high-DPI pages. `(0, 0)` is the
viewport's top-left corner. Map resized previews back to the original size, and
do not add document scroll offsets. `screenshot --full-page` still exports the
whole page, but `mouse` cannot use its coordinates directly.

`mouse` takes `click`, `dblclick`, `move`, `scroll` or `drag`, and requires
`x y`. `--button` is `left` (default), `middle` or `right` for click,
double-click and drag. Drag requires both `--end-x` and `--end-y`. Scroll uses
`--delta-x` and `--delta-y` over the element under `x y`. Positive vertical
deltas scroll down, also in nested scroll areas. Non-finite, out-of-viewport
and unused arguments fail with `usage`. Coordinate input sends direct browser
events. Selector-based `click` and `type` keep their human timing and
covered-target checks.

`insert-text` inserts literal Unicode into the focused control with an input
event. It does not treat `Enter` as a key or send per-character keydown and
keyup events. Focus the control with a click or Tab. Use `press` for keys and
shortcuts, or selector-based `type` for controls that need real keystrokes.

Take a fresh view after navigation, scrolling or layout changes. Wheel input can
return before scrolling ends, so wait for the expected page state. Prefer refs
and selectors for known controls, and run dependent actions in sequence. The
view shows the page viewport, not browser chrome or OS dialogs.

### Chromium sandbox

`open` keeps Chromium's OS sandbox, which contains the renderer if a hostile
page compromises it. Some environments cannot start it, such as containers without
unprivileged user namespaces. There the browser exits at startup with a message
that says so.

```bash
webskrap browser open --no-sandbox            # this session only
WEBSKRAP_CHROMIUM_SANDBOX=0 webskrap browser open   # every session on this host
```

`--no-sandbox` wins over the environment variable, and a session without the
sandbox prints a warning. `webskrap browser list` shows a `Sandbox` column for
running sessions. Fix the host if you can: the sandbox is the difference
between a compromised renderer and a compromised machine. WebSkrap never drops
the sandbox on its own, and never retries a failed launch without it.

### Proxy

Pass the proxy when the browser starts:

```bash
webskrap browser open --proxy socks5://proxy.example:1080
webskrap browser open --proxy http://proxy.example:8080 \
  --webrtc-ip-handling-policy default_public_interface_only
```

With `--proxy`, WebRTC defaults to `disable_non_proxied_udp`, so ICE candidates
show neither the host's LAN address nor its direct public address.
`--webrtc-ip-handling-policy` overrides it, with or without a proxy. Both
settings are fixed at launch.

For an http or https proxy that asks for a password, put the credentials in the
environment, not in the URL:

```bash
export WEBSKRAP_PROXY_USERNAME=user
read -rs WEBSKRAP_PROXY_PASSWORD && export WEBSKRAP_PROXY_PASSWORD
webskrap browser open --proxy http://proxy.example:8080
```

WebSkrap starts a helper process that keeps the credentials in memory, answers
the proxy for every page, frame and worker, and stops when the browser closes.
The credentials never go to disk, to a command line, or to the browser's
environment. SOCKS proxies cannot use credentials, because Chromium does not
answer their challenge. If the helper stops, later commands fail with
`session_unreachable`. Close the session and open it again.

Reopening a running session with a different proxy or policy fails with a
`usage` error instead of reusing a browser that bypasses the proxy. Reopening it
without `--proxy` reuses it as it is. `webskrap browser list` shows each running
session's proxy, with `(auth)` when the helper answers it.

### Tabs

Commands act on the session's current tab:

```bash
webskrap browser tab-list              # * marks the current tab
webskrap browser tab-new https://example.com
webskrap browser tab-select 0          # index from tab-list, in opening order
webskrap browser tab-close             # the current tab, or pass an index
```

A popup or new tab becomes current. When it closes, the tab selected before it
is current again. When the current tab closes, the newest remaining tab is
current.

### Limits

Unlike the official Playwright CLI, `webskrap browser` supports only
Chromium-based browsers and has no commands for network mocking, tracing or
video. `back` and `forward` always reload the page, because the back/forward
cache is off to keep history navigation deterministic.

The [MCP server](/docs/user-guide/mcp/#interactive-browser-tools) exposes the
same sessions to agents as `browser_*` tools.
