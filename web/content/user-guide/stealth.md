---
title: Stealth Browser Scraping with Patchright
description: Use Patchright-powered browser sessions in WebSkrap for stealth-oriented Python web scraping without CAPTCHA or access-control bypassing.
---

# Stealth

WebSkrap's stealth path is the Patchright driver.

Patchright is a CDP-leak-free Playwright fork. WebSkrap does not inject
JavaScript fingerprint patches; it relies on real browser behavior, persistent
contexts, and coherent profile settings instead.

## Patchright

Use Patchright for CDP-aware detection surfaces. `pip install webskrap` includes
Patchright; `webskrap install` downloads its browser:

```bash
pip install webskrap
webskrap install
```

```python
from webskrap import SessionConfig

config = SessionConfig(
    driver="patchright",
    channel="chrome",
    headless=False,
)
```

Patchright works best with real Chrome and a persistent context. If
`user_data_dir` is omitted, WebSkrap creates a temporary persistent profile for
Patchright sessions. Patchright contexts use the browser's real viewport and
disable synthetic focus control (`no_viewport=True`, `focus_control=False`).
The CLI omits `focus_control` for compatibility with Patchright versions that do
not accept that option.

## Context profile mode

Patchright defaults to the host browser's native locale, timezone, headers, and
media preferences. This is the strict anti-bot mode because fewer context
overrides means fewer behavioral mismatches.

Fingerprint-statistics pages such as AmiUnique also score how common each
browser-visible attribute is. For those cases, you can opt in to applying the
selected profile's context metadata while still avoiding viewport, user-agent,
and JavaScript fingerprint patches. This can align language, timezone, and media
metadata, but it does not make high-entropy fingerprints non-unique by itself:

```python
from webskrap import SessionConfig

config = SessionConfig(
    driver="patchright",
    channel="chrome",
    headless=True,
    virtual_display=True,
    patchright_context_profile=True,
    reduce_fingerprint_surface=True,
    webrtc_ip_handling_policy="disable_non_proxied_udp",
)
```

This applies `locale`, `timezone_id`, `navigator_languages`, `color_scheme`,
`reduced_motion`, and any caller-provided `extra_http_headers`. It keeps
`no_viewport=True`, so screen and window metrics still come from the browser or
from `headless_screen`.

On Linux the timezone and languages are set the way a real machine
sets them, not overridden over CDP. The browser starts with `TZ` set to the
profile's `timezone_id`, `LC_ALL`/`LANG`/`LANGUAGE` derived from its locale and
languages, and Chromium's `--accept-lang` flag carrying `navigator_languages`.
So `navigator.languages` holds the whole list (`["fr-FR", "fr", "en-US", "en"]`,
not just `["fr-FR"]`), Chromium writes the `Accept-Language` q-values itself, and
`Intl` uses the profile locale. An unknown `timezone_id` is rejected before
launch, because Chromium would report `Etc/Unknown`. A `--accept-lang` passed
in `launch_args` wins. macOS and Windows take the locale from system settings
instead, so there the profile is still applied through CDP overrides.

Pick a `timezone_id` and languages that match your exit IP. A server left on
UTC is the most common reason a clean browser still reads as a datacenter;
`webskrap doctor` warns when the host timezone is UTC.

Set `webrtc_ip_handling_policy="disable_non_proxied_udp"` when leak-test pages
should not see local or direct public WebRTC ICE candidates. WebSkrap applies
Chromium's native process flags (`--webrtc-ip-handling-policy` plus
`--force-webrtc-ip-handling-policy`) instead of patching `RTCPeerConnection`.
The supported policy values are `default`,
`default_public_and_private_interfaces`, `default_public_interface_only`, and
`disable_non_proxied_udp`.

WebRTC IP handling only controls ICE candidates. It will not hide the page's
normal remote address, and it will not normalize unrelated fingerprint surfaces
such as fonts, canvas, battery, device memory, or TLS/session metadata.

Set `reduce_fingerprint_surface=True` to ask Chromium to disable WebGL and
canvas readback with native flags (`--disable-webgl` and
`--disable-reading-from-canvas`). This reduces rendering entropy on
fingerprint-statistics pages, but pages that require WebGL or canvas export may
not work correctly.

## Headless patchright

Headless mode is more detectable than headed mode. For best-effort headless
stealth, prefer real Chrome and a stable profile:

```python
from pathlib import Path

from webskrap import SessionConfig

config = SessionConfig(
    driver="patchright",
    channel="chrome",
    headless=True,
    user_data_dir=Path(".webskrap/headless-profile"),
)
```

WebSkrap intentionally keeps headless browser surfaces native instead of spoofing
them with JavaScript. Broad fingerprint patches often become tampering signals.

Headless Chrome has no physical display, so screen and window metrics
(`screen.width`, `window.outerWidth`, ...) otherwise leak as headless tells,
defaulting to an 800x600 screen with zero outer dimensions. For chromium headless
runs WebSkrap configures a virtual screen at launch via browser flags
(`--screen-info`, `--window-size`, `--window-position`), so the page reports
coherent display metrics. This is a real browser-level screen, not JavaScript
spoofing, so it does not register as tampering. The default is 1920x1080; set
`headless_screen` to a `Viewport` to change it, or to `None` to disable:

```python
from webskrap import SessionConfig, Viewport

config = SessionConfig(
    driver="patchright",
    channel="chrome",
    headless=True,
    headless_screen=Viewport(width=1366, height=768),
)
```

The other headless tell that survives patchright is headless mode itself:
Chrome stamps `HeadlessChrome` into `navigator.userAgent` and the worker UA
(including `SharedWorker`, which runs in its own process), and Playwright only
hides scrollbars and forces hover/pointer media types in headless mode.

### Virtual display

On Linux, set `virtual_display=True` to run the browser headed on a private
Xvfb server instead of in headless mode. Nothing is overridden, so the page sees
a normal headed Chrome: no `HeadlessChrome` token, the full set of user-agent
client hints, real scrollbars, and an X screen sized by `headless_screen`
(1920x1080 when that is `None`). The window stays invisible.

```python
config = SessionConfig(
    driver="patchright",
    channel="chrome",
    headless=True,
    headless_screen=Viewport(width=1366, height=768),
    virtual_display=True,
)
```

WebSkrap starts one Xvfb per session and stops it when the session closes (a
process killed with SIGKILL leaves its Xvfb running). The
server listens on no TCP port and only accepts clients holding a random
MIT-MAGIC-COOKIE stored in an owner-only file, so other local users cannot
connect to it. It needs the `Xvfb` binary (Debian/Ubuntu: `apt install xvfb`);
without it, or off Linux, the session fails to start with a `browser_launch`
error. `virtual_display` only applies when `headless=True`.

### Masking the user agent

`mask_headless_user_agent=True` keeps headless mode and rewrites
`HeadlessChrome` to `Chrome` instead. WebSkrap probes the real UA once, then
applies the cleaned value with Chromium's `--user-agent` launch flag, which
covers the page, every worker, and request headers. The cost is client hints:
with a command-line UA override, Chromium reports only the low-entropy hints, so
`navigator.userAgentData.getHighEntropyValues()` and the
`Sec-CH-UA-Full-Version-List`, `-Arch`, `-Bitness` and `-Platform-Version`
headers come back empty, which a real Chrome never does. Prefer
`virtual_display` where Xvfb is available. The mask is ignored when
`virtual_display` is set, and it is off by default so headless stays honestly
headless unless you opt in.

## Practical guidance

- Reuse persistent sessions when realistic continuity matters.
- Keep locale, timezone, and languages coherent.
- Prefer an installed browser channel such as `chrome` when testing headed behavior.
- Treat headless stealth as best-effort; headed Patchright, or `virtual_display`
  on Linux, remains the strict mode.
- Avoid randomizing every request; incoherent changes can look less realistic.

## Recipes

Headed Chrome for the strictest Patchright path:

```python
from pathlib import Path

from webskrap import SessionConfig

config = SessionConfig(
    driver="patchright",
    channel="chrome",
    headless=False,
    user_data_dir=Path(".webskrap/patchright-profile"),
)
```

Invisible headed browser on a private Xvfb display (Linux):

```python
from pathlib import Path

from webskrap import SessionConfig, Viewport

config = SessionConfig(
    driver="patchright",
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
    driver="patchright",
    channel="chrome",
    headless=True,
    patchright_context_profile=True,
    reduce_fingerprint_surface=True,
    webrtc_ip_handling_policy="disable_non_proxied_udp",
)
```

Run the live test suite only when you intentionally want to hit third-party demo
sites:

```bash
WEBSKRAP_LIVE=1 pytest -q -m live
```
