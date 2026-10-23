# Own CDP driver

Status: experimental. One-shot fetch only.

## Goal

WebSkrap drives Chromium through Patchright, a patched Playwright. This
project replaces that stack with WebSkrap's own Chrome DevTools Protocol (CDP)
client, in stages. Each stage must match Patchright on detection before the
next stage starts.

Reasons:

1. Fewer dependencies. The driver needs no Playwright, no Patchright, no
   Node.js process, and no WebSocket library.
2. Full control of every CDP command that the browser receives.
3. No wait for Patchright to follow each Playwright release.

## Stages

1. CDP core and a one-shot fetch backend. **Done on this branch.**
2. Measure against Patchright on the same host and IP. **Done; see below.**
3. Port persistent browser sessions: DOM and accessibility snapshot refs, and
   input through `Input.dispatch*` events.
4. Make Playwright optional after full parity. Remove it in a major version.

Stop at the first stage that does not match Patchright on detection.

## Design

`src/webskrap/cdp.py` is the core:

- **Transport.** Chromium starts with `--remote-debugging-pipe`. It reads
  NUL-terminated JSON on file descriptor 3 and writes on descriptor 4.
  `os.posix_spawn` maps the descriptors. The browser opens no TCP port, so no
  other local process can attach.
- **Connection.** `CDPConnection` sends commands with increasing ids, matches
  replies, and routes events by session id and method. When the pipe closes,
  all pending commands fail with a launch or sandbox error.
- **Pages.** `CDPBrowser.new_page` creates a target and attaches to it with a
  flat session.
- **Scripts.** `CDPPage.evaluate` creates an isolated world and runs
  `Runtime.evaluate` in it. The driver never sends `Runtime.enable` or
  `Console.enable`. Page code cannot see or patch the objects that the scripts
  use. The page's Content Security Policy does not block these scripts.
- **Launch flags.** The command line is short: the pipe, the profile, first-run
  suppression, `--disable-field-trial-config`, and the flags that a Patchright
  fetch gets from `SessionConfig.launch_options`. The driver also adds
  `--disable-blink-features=AutomationControlled`, because no driver patch
  hides `navigator.webdriver`.

`src/webskrap/cdp_fetch.py` builds `fetch()` on the core. It returns the same
`FetchResult` as `WebSkrapClient.fetch`. It uses these CDP domains on the page:

- `Page`: navigation and lifecycle events.
- `Network`: status and headers of the main document.
- `Fetch`: only when a resource policy blocks resource types, and then only
  for those types.

## Measured results

Host: Linux ARM64, bundled Chromium, sandbox off, same IP for both drivers.

Local probe page (`scripts/compare_drivers.py`). It checks `navigator.webdriver`,
the console `Runtime.enable` probe, the user agent, client-hint brands,
`window.chrome`, plugins, languages, screen, window, WebGL, and automation
globals.

| Mode | Signals | Patchright median | CDP median |
| --- | --- | --- | --- |
| headless | identical | 1527 ms | 977 ms |
| virtual display | identical | 1798 ms | 1233 ms |

Public demos (the targets of the live test suite), same wait for both drivers:

| Demo | Mode | Patchright | CDP |
| --- | --- | --- | --- |
| deviceandbrowserinfo | headless | `isBot: true` (headless signals) | same |
| deviceandbrowserinfo | virtual display | `isBot: false`, no CDP or webdriver flag | same |
| bot.sannysoft.com | virtual display | WebGL vendor and renderer fail | same |
| bot.incolumitas.com | virtual display | `WEBDRIVER` fails | same |

Protected pages (`scripts/driver_parity.py`), 3 fresh-profile runs per driver
and mode, 30 to 90 s per run. Runs alternate which driver goes first.

| Target | Mode | Patchright | CDP |
| --- | --- | --- | --- |
| Cloudflare challenge (scrapingcourse.com, 2 pages), wait only | headless, virtual display | 0/3 each, checkbox stays | same |
| Same, with a checkbox click (`--click`) | headless | 0/3 each, checkbox returns | same |
| Same, with a checkbox click (`--click`) | virtual display | 3/3 each, median 8.8 s | 3/3 each, median 7.3 to 8.2 s |
| Cloudflare Turnstile (peet.ws) | headless, virtual display | 3/3 token | 3/3 token |
| reCAPTCHA v3 demo | headless, virtual display | 3/3, score 0.9 | 3/3, score 0.9 |
| DataDome-protected form (local test) | headless, virtual display | 3/3 captcha, 0 blocked | 3/3 captcha, 0 blocked |

On this datacenter (VPS) IP, DataDome always shows its captcha; the IP
reputation alone triggers it. So the captcha counts as a pass here, and only
the hard block (`t=bv`) counts as a failure. The hard block never appeared.
The captcha is not solved.

Cloudflare needs the "Verify you are human" checkbox clicked; waiting alone
never clears it. `--click` moves both drivers along the same curved path
through CDP input events. The click clears the challenge in a headed
browser on a virtual display, but not in headless mode.

Result: the CDP driver matches Patchright on every signal measured. It is
about 30 to 35 percent faster per cold fetch. It does not beat Patchright on
detection. The remaining failures come from the host (no GPU, headless mode),
not from the driver.

One difference was found and fixed. Chromium builds apply their testing field
trials by default, and one trial trims `navigator.languages` to one entry.
Playwright passes `--disable-field-trial-config`, so the CDP driver now does
the same.

## Limits

- Linux and macOS only, because of `posix_spawn` with descriptor mapping.
- One page per fetch. No auto-attach to out-of-process iframes or workers, so
  a resource policy does not cover requests that they make.
- `ready_selector` takes CSS only. Playwright engines such as `text=` and
  `role=` do not work.
- No consent dismissal, proxy credentials, `storage_state`, JavaScript off,
  service-worker blocking, `slow_mo_ms`, `mask_headless_user_agent`, or
  `patchright_context_profile`. Each raises `usage`. See `UNSUPPORTED_FIELDS`
  in `cdp_fetch.py`.
- Profile settings are not applied. A Patchright fetch also ignores them
  unless `patchright_context_profile` is set, so both drivers present the
  browser's native identity.
- `PLAYWRIGHT_BROWSERS_PATH` is honored, including `0` (package-local
  browsers). The driver finds Chromium without starting Playwright.
- `webskrap fetch --driver cdp` is the only CLI entry point. The MCP server,
  `search`, and `WebSkrapClient` still use Patchright.

## Conditions for more work

- Parity held on Cloudflare and DataDome over 3 runs per mode (stage 2 gate
  met). On this datacenter host, neither driver clears the Cloudflare
  challenge pages without a click or in headless mode, and DataDome always
  shows its captcha (expected from the IP). So stage 3 gains speed and
  control, not stealth. A residential IP or a GPU host is the next lever for
  detection.
- Proxy credentials need `Fetch.authRequired`. That pauses every request, so
  add it only when a user needs an authenticated proxy.
- Consent dismissal needs click and locator primitives. They come with stage 3.
