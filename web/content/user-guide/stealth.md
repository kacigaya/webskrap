---
title: Stealth Browser Scraping with Python
description: Use WebSkrap's own CDP client for stealth-oriented Python web scraping, with opt-in Cloudflare Turnstile, DataDome slider, HUMAN Press & Hold and reCAPTCHA/hCaptcha checkbox handling and no access-control bypassing.
---

# Stealth

WebSkrap drives Chromium through its own Chrome DevTools Protocol (CDP) client.
It never enables the `Runtime` or `Console` domains, so detectors never see the
`Runtime.enable` leak. Its page scripts run in isolated worlds. WebSkrap injects
no JavaScript fingerprint patches. It relies on real browser behavior,
persistent profiles and coherent profile settings.

## Establish a baseline

Before you compare configurations, check `webskrap doctor --format json` and the
MCP `doctor` result. Its `fingerprint` and `warnings` list the uncommon values a
default fetch shows pages, each with a native fix. Compare package versions and
executable paths too: a connected MCP process can run a different installation
from the checkout you edit. Restart that process on the intended installation
before you test a code change.

Keep the browser version, persistent profile and network stable during a
comparison. Start with all resources and native surfaces:
`resource_policy="all"`, `mask_headless_user_agent=False` and
`reduce_fingerprint_surface=False`. On Linux with Xvfb, compare that headless
baseline with `virtual_display=True`. Keep languages and timezone consistent
with the real browsing environment. None of these controls guarantees access to
a particular site.

If Chromium reports a sandbox error, check user namespaces and any AppArmor
policy for its real executable path before you test stealth. A policy for a
system Chrome binary may not cover downloaded Chromium. Restore sandbox support
instead of changing fingerprint options. WebSkrap never disables the sandbox on
its own.

Known challenge and block pages raise `blocked`, even with HTTP 200:

- Challenges: DataDome, Cloudflare "Just a moment", Akamai's sec-cpt page,
  Fastly's Client Challenge (a `/_fs-ch-*/script.js` loader), and AWS WAF's
  challenge or CAPTCHA action (`x-amzn-waf-action`).
- Blocks: Cloudflare's own error pages for codes 1005-1010, 1012, 1015 and
  1020, a site-styled Cloudflare 403 page that shows the response's Ray ID,
  and an Akamai edge page (`server: AkamaiGHost`, HTTP 400 or 403) with an
  Akamai reference number.

The Akamai sec-cpt page, Fastly's Client Challenge and the AWS WAF challenge
action reload the page from their own script. WebSkrap waits up to 15 seconds
for that reload, then reads the real page. It reports `blocked` only if the
interstitial stays. A plain 403, 404 or 202 without these provider markers is
not a block: it returns `ok: false` or `ok: true` as usual. WebSkrap never uses
the localized title of a block page as a signal.

The `blocked` hint depends on the kind of page. For a block, change the exit IP
or try `virtual_display=True`. For a challenge, try `virtual_display=True`,
headed mode, or a persistent `user_data_dir`. On Linux with Xvfb installed, the
hint names `virtual_display=True` first. Errors carry no page text, IP
addresses, Ray IDs or reference IDs. Detection cannot find every challenge or
promise that later interactions succeed. Stop automatic retries on `blocked`.

Some sites refuse a datacenter exit IP whatever the browser does. Only a
residential or mobile exit IP helps there.

A successful HTTP status does not prove the intended page loaded. Pass
`ready_selector` to require a visible content element before extraction.

## Challenge solvers

Four opt-in options each answer one widget. All are off by default, so without
them the challenge page fails fast with `blocked`. With one on, WebSkrap waits
on the challenge page, reads the widget's position from its DOM before each
attempt, and moves the cursor like a person. The widget gets up to 60 seconds
from when it first appears, on top of `timeout_ms`. The call succeeds only when
the real page loads and no challenge is detected on it. Otherwise it fails with
`blocked`.

An answer only answers the widget. The provider still scores the browser and
the exit IP, so a headless browser or a flagged IP can still be refused. No
token or cookie is read, copied or injected, and no outside solving service is
used. Image and audio puzzles stay `blocked` and are never touched.

### Cloudflare Turnstile

`solve_cloudflare=True` (CLI `--solve-cloudflare`) handles Cloudflare's "Just a
moment" page. When the Turnstile checkbox appears, the cursor wanders for 1.5 to
3 seconds, follows a curved path to the checkbox, and clicks with a mouse-like
button hold. If the page has not cleared 20 seconds later, WebSkrap clicks
again, at most three times. It works in headless, headed and `virtual_display`
modes.

### DataDome slider

`solve_datadome=True` (CLI `--solve-datadome`) handles DataDome's slider
CAPTCHA. When the slider appears, the cursor wanders for 3 to 5 seconds, grabs
the handle, and drags it onto the slot in 35 small moves: slow, then faster,
then slow again, with a little jitter. If the page has not cleared 10 seconds
later, WebSkrap drags again, at most three times.

DataDome also scores the gesture. A hard block has no slider and stays
`blocked`. Headed and `virtual_display` browsers passed in live runs. A headless
browser can get the hard block instead.

### HUMAN Press & Hold

`solve_perimeterx=True` (CLI `--solve-perimeterx`) handles the Press & Hold
challenge of HUMAN Security (formerly PerimeterX). The cursor follows a curved
path to the button, presses, and holds with a small hand tremor until the
progress fill is complete, then lets go. If the page has not cleared 8 seconds
later, WebSkrap holds again, at most three times.

In live runs, HUMAN refused every completed hold from a flagged IP, in every
browser mode, and accepted the first hold from a cleaner exit.

### reCAPTCHA and hCaptcha checkbox

`solve_recaptcha=True` (CLI `--solve-recaptcha`) clicks the "I'm not a robot"
checkbox of reCAPTCHA v2 or hCaptcha on a page that blocks the site: a 403, 429
or 503 response that carries the widget. When the provider trusts the browser,
the box ticks and the page's own callback continues to the real page. If the
page has not cleared 15 seconds later, WebSkrap clicks again, at most twice
within 60 seconds. When the provider opens an image challenge instead, the call
fails with `blocked` at once.

For a checkbox inside a form, fill the form, then call `browser_solve_captcha`
(CLI `webskrap browser solve-captcha`) before you submit it. Its `captcha.state`
is `checked`, `challenge`, `gone`, `timeout` or `not_found`.

A datacenter IP usually gets the image challenge. reCAPTCHA v3 and invisible
widgets have no checkbox and are not handled.

Use these options only on sites whose terms and applicable law allow automated
access, and at a polite rate. Passing a challenge does not give permission to
collect data you may not collect.

## Browser channel

Real Chrome is the strongest channel. On hosts without Chrome,
`webskrap install` downloads Chrome for Testing:

```bash
pip install webskrap
webskrap install
```

```python
from webskrap import SessionConfig

config = SessionConfig(
    channel="chrome",
    headless=False,
)
```

Without `user_data_dir`, WebSkrap creates a temporary profile per session and
removes it when the session closes. Pages use the browser's real window size.
No viewport is emulated by default.

## Profile mode

The default, `profile_mode="native"`, uses the host browser's own locale,
timezone, headers and media preferences. It is the strict anti-bot mode: fewer
overrides mean fewer mismatches.

Fingerprint-statistics pages such as AmiUnique also score how common each
visible attribute is. For those, you can apply the selected profile's context
metadata and still avoid viewport, user-agent and JavaScript fingerprint
patches. This aligns language, timezone and media metadata, but does not make
high-entropy fingerprints common on its own:

```python
from webskrap import SessionConfig

config = SessionConfig(
    channel="chrome",
    headless=True,
    virtual_display=True,
    profile_mode="locale",
    reduce_fingerprint_surface=True,
    webrtc_ip_handling_policy="disable_non_proxied_udp",
)
```

`profile_mode="locale"` applies `locale`, `timezone_id`, `navigator_languages`,
`color_scheme`, `reduced_motion`, and any `extra_http_headers` you pass. On
Linux, locale and timezone come from the browser's environment, so nothing is
overridden over CDP. Screen and window metrics still come from the browser or
from `headless_screen`. `profile_mode="full"` also emulates the viewport,
screen, device scale, touch and user agent, as WebSkrap 2.x did. Each override
is a surface a page can cross-check.

A profile user agent comes with matching client hints: `navigator.userAgentData`
and the `Sec-CH-UA` headers carry its platform, platform version, model, mobile
flag and Chrome version. The brand list follows Chromium's rules for that
version and names only the running browser: `Google Chrome` for the `chrome`
channel, `Microsoft Edge` for `msedge`, and no brand for Chromium. The full
version is the browser's own when the user agent has the same Chrome major
version. Otherwise it is `<major>.0.0.0`, so keep the two the same. A user agent
without a `Chrome/` token gets no client hints.

The overrides also reach cross-site iframes and workers. Each one starts paused,
gets the profile, then runs. Two limits remain:

- Chromium applies the screen and device-scale override to top-level pages
  only. A cross-site iframe reports the browser's real screen size and pixel
  ratio.
- A shared worker reports the browser's own `navigator.userAgent`. Use
  `mask_headless_user_agent` to change the user agent for the whole browser.

On Linux, WebSkrap sets timezone and languages the way a real machine does,
not over CDP. The browser starts with `TZ` set to the profile's `timezone_id`,
`LC_ALL`/`LANG`/`LANGUAGE` derived from its locale and languages, and
Chromium's `--accept-lang` flag carrying `navigator_languages`. So
`navigator.languages` holds the whole list (`["fr-FR", "fr", "en-US", "en"]`,
not only `["fr-FR"]`), Chromium writes the `Accept-Language` q-values itself,
and `Intl` uses the profile locale. WebSkrap rejects an unknown `timezone_id`
before launch, because Chromium would report `Etc/Unknown`. A `--accept-lang`
in `launch_args` wins. macOS and Windows take the locale from system settings,
so there WebSkrap still applies the profile through CDP overrides.

Pick a `timezone_id` and languages that match your exit IP. A server left on
UTC is the most common reason a clean browser still reads as a datacenter.
`webskrap doctor` warns when the host timezone is UTC.

Set `webrtc_ip_handling_policy="disable_non_proxied_udp"` to hide local and
direct public WebRTC ICE candidates from leak-test pages. WebSkrap applies
Chromium's native flags (`--webrtc-ip-handling-policy` plus
`--force-webrtc-ip-handling-policy`) and does not patch `RTCPeerConnection`.
The supported values are `default`, `default_public_and_private_interfaces`,
`default_public_interface_only` and `disable_non_proxied_udp`.

Behind a proxy this is the default. A session with `proxy` set and no explicit
policy gets `disable_non_proxied_udp`. Otherwise ICE still gathers the host's
LAN address and, through STUN, its direct public address: the address the
proxy is meant to hide. Pass `webrtc_ip_handling_policy="default"` to keep
Chromium's behavior behind a proxy.

Persistent sessions take the same controls.
`webskrap browser open --proxy socks5://proxy.example:1080` routes all session
traffic through the proxy and blocks non-proxied UDP.
`--webrtc-ip-handling-policy` overrides the policy. Both are fixed when the
browser starts, so WebSkrap refuses to reopen a running session with a
different proxy or policy instead of reusing a browser that bypasses the proxy.
Persistent sessions refuse credentials in the proxy URL. For an http or https
proxy, set `WEBSKRAP_PROXY_USERNAME` and `WEBSKRAP_PROXY_PASSWORD` instead (see
the [CLI guide](cli.md#proxy)). SOCKS proxies cannot use
credentials. One-shot sessions take credentials through `ProxyConfig`.

WebRTC IP handling only controls ICE candidates. It does not hide the page's
normal remote address or normalize other surfaces such as fonts, canvas,
battery, device memory, or TLS and session metadata.

`reduce_fingerprint_surface=True` disables WebGL and canvas readback with
Chromium's native flags (`--disable-webgl` and `--disable-reading-from-canvas`).
This lowers rendering entropy on fingerprint-statistics pages, but pages that
need WebGL or canvas export can break. A browser with neither is also rare, and
fingerprinting audits report it as blocking. To hide the SwiftShader renderer
and keep WebGL, use `gpu="mesa"` (see [WebGL renderer](#webgl-renderer)).

## Headless mode

Headless mode is easier to detect than headed mode. For best-effort headless
stealth, use real Chrome and a stable profile:

```python
from pathlib import Path

from webskrap import SessionConfig

config = SessionConfig(
    channel="chrome",
    headless=True,
    user_data_dir=Path(".webskrap/headless-profile"),
)
```

WebSkrap keeps headless browser surfaces native and does not spoof them with
JavaScript. Broad fingerprint patches often become tampering signals.

Canvas, audio and WebGL fingerprints are not randomized between requests. They
stay stable for the same browser, profile and rendering environment. A new
browser version, GPU backend, font set or host can change them.
`reduce_fingerprint_surface=True` disables canvas readback and WebGL instead of
rotating their values.

Headless Chrome has no physical display. Without help, it reports an 800x600
screen with zero outer dimensions, and `screen.width`, `window.outerWidth` and
similar values give it away. For Chromium headless runs, WebSkrap sets a
virtual screen at launch with `--screen-info`, `--window-size` and
`--window-position`. The work area leaves room for a common Linux desktop panel
(GNOME, Ubuntu, Xfce or Plasma), and the window is maximized in it or floats
inside it. A fixed geometry would be one more value shared by every WebSkrap
user, so the panel and window come from the persistent profile directory: the
same profile keeps the same desktop. Runs without a persistent profile share
one geometry for the life of the process. On a virtual display the X server is
the screen, with no panel. The default screen is 1920x1080. Set
`headless_screen` to a `Viewport` to change it, or to `None` to turn it off:

```python
from webskrap import SessionConfig, Viewport

config = SessionConfig(
    channel="chrome",
    headless=True,
    headless_screen=Viewport(width=1366, height=768),
)
```

The other tell is headless mode itself. Chrome stamps `HeadlessChrome` into
`navigator.userAgent` and the worker user agent, including `SharedWorker`,
which runs in its own process. Headless Chromium also has no input devices, so
it reports no hover and no fine pointer. WebSkrap declares a mouse with
`--blink-settings` in headless mode. It does not hide scrollbars or mute audio.

`fake_media_devices=True` in `SessionConfig`, or `--fake-media-devices` on
`fetch`, `search` or `browser open`, turns on Chromium's synthetic camera and
microphone. Permission prompts stay active. Sites can detect the synthetic
devices, so it is off by default.

### Virtual display

On Linux, `virtual_display=True` runs the browser headed on a private Xvfb
server instead of in headless mode. Nothing is overridden, so the page sees a
normal headed Chrome: no `HeadlessChrome` token, the full set of user-agent
client hints, real scrollbars, and an X screen sized by `headless_screen`
(1920x1080 when that is `None`). The window stays invisible.

```python
config = SessionConfig(
    channel="chrome",
    headless=True,
    headless_screen=Viewport(width=1366, height=768),
    virtual_display=True,
)
```

WebSkrap starts one Xvfb per session and stops it when the session closes. A
process killed with SIGKILL leaves its Xvfb running. The server listens on no
TCP port and accepts only clients that hold a random MIT-MAGIC-COOKIE from an
owner-only file, so other local users cannot connect. It needs the `Xvfb`
binary (Debian/Ubuntu: `apt install xvfb`). Without it, or off Linux, the
session fails with a `browser_launch` error. `virtual_display` applies only
when `headless=True`.

### Masking the user agent

`mask_headless_user_agent=True` keeps headless mode and rewrites
`HeadlessChrome` to `Chrome`. WebSkrap probes the real user agent once, then
applies the cleaned value with Chromium's `--user-agent` flag, which covers the
page, every worker and request headers. The cost is client hints. With a
command-line override, Chromium reports only low-entropy hints, so
`navigator.userAgentData.getHighEntropyValues()` and the
`Sec-CH-UA-Full-Version-List`, `-Arch`, `-Bitness` and `-Platform-Version`
headers come back empty, which real Chrome never does. Prefer `virtual_display`
where Xvfb is available. The mask is ignored when `virtual_display` is set, and
it is off by default, so headless stays honestly headless unless you opt in.

### Client hint brands

The `Sec-CH-UA` header and `navigator.userAgentData.brands` name the browser
vendor. Chrome sends `Google Chrome`, Edge sends `Microsoft Edge`, and bare
Chromium sends only `Chromium` and a GREASE brand such as `Not_A Brand`. The
user agent says `Chrome` in all three. Some detectors score a missing vendor
brand as a bot signal, but real Chromium users send the same header, so the
signal is weak.

WebSkrap does not fake the brand. A fake brand needs a per-page CDP override
that does not reach every worker, so pages can compare the two values. To send
a vendor brand, install Google Chrome or Microsoft Edge and keep
`channel="chrome"`. Google Chrome has no Linux ARM64 build, so ARM64 Linux hosts
fall back to Chromium. `webskrap doctor` warns when it resolves to Chromium.

### WebGL renderer

Without a usable GPU, headless Chrome renders WebGL with SwiftShader, and pages
can read that through `WEBGL_debug_renderer_info`:
`ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device ...), SwiftShader driver)`.
Chrome 139 removed the automatic SwiftShader fallback from regular Chrome, so a
real browser no longer reports it, and detectors treat it as automation.

On Linux, `gpu="mesa"` renders through Mesa's software Vulkan driver (lavapipe)
instead, with Chromium's own flags (`--use-gl=angle --use-angle=vulkan
--ignore-gpu-blocklist`): `ANGLE (Mesa, Vulkan ... (llvmpipe ...), llvmpipe)`.
That string is uncommon, because stock Chrome blocklists software drivers, but
it is not specific to automation. It needs the lavapipe driver
(Debian/Ubuntu: `apt install mesa-vulkan-drivers`). Without it, the session
fails with a `browser_launch` error. `webskrap doctor` reports whether it is
installed. Keep the default `gpu="auto"` on a machine with a real GPU.

```python
config = SessionConfig(
    channel="chrome",
    headless=True,
    gpu="mesa",
)
```

Under `virtual_display`, headed Chrome with no usable GPU has no WebGL at all.
A browser without WebGL is rare, and bot tests flag the missing WebGL vendor
and renderer. So under `virtual_display`, `gpu="auto"` launches with
`gpu="mesa"` when lavapipe is installed and no Intel, AMD or NVIDIA GPU is found
(a DRM render node with one of those PCI vendors, or `/dev/nvidia0`). Without
lavapipe, WebGL stays off. Other GPUs, such as an ARM SoC GPU, are not detected
and also get Mesa. Pass `launch_args=["--use-angle=gl"]` to keep Chromium's
Linux default. `browser_open` and `webskrap browser open` report the backend
the browser launched with. WebGPU is left alone: its adapter stays `null`,
which is what Chrome reports on machines without a supported GPU. Your own
`--use-gl` and `--use-angle` flags take precedence.

## Practical guidance

- Reuse persistent sessions when continuity matters.
- Keep locale, timezone and languages coherent.
- Use an installed channel such as `chrome` to test headed behavior.
- Treat headless stealth as best-effort. Headed Chrome, or `virtual_display`
  on Linux, is the strict mode.
- Do not randomize every request. Incoherent changes look less realistic.

## Recipes

Headed Chrome, the strictest path:

```python
from pathlib import Path

from webskrap import SessionConfig

config = SessionConfig(
    channel="chrome",
    headless=False,
    user_data_dir=Path(".webskrap/headed-profile"),
)
```

Invisible headed browser on a private Xvfb display (Linux):

```python
from pathlib import Path

from webskrap import SessionConfig, Viewport

config = SessionConfig(
    channel="chrome",
    headless=True,
    user_data_dir=Path(".webskrap/headless-profile"),
    headless_screen=Viewport(width=1366, height=768),
    virtual_display=True,
)
```

Fingerprint-statistics and WebRTC leak-test mode:

```python
config = SessionConfig(
    channel="chrome",
    headless=True,
    profile_mode="locale",
    reduce_fingerprint_surface=True,
    webrtc_ip_handling_policy="disable_non_proxied_udp",
)
```

Run the live test suite only when you mean to hit third-party demo sites:

```bash
WEBSKRAP_LIVE=1 pytest -q -m live
```
