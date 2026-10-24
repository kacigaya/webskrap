# Own CDP driver

Status: the only driver since 3.0. Patchright and Playwright are removed.

## Goal

WebSkrap 2.x drove Chromium through Patchright, a patched Playwright. This
project replaced that stack with WebSkrap's own Chrome DevTools Protocol (CDP)
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
3. Port persistent browser sessions. **In progress; see the stage 3 plan.**
4. Remove Playwright and Patchright in v3.0.0. **In progress; see the v3 plan.**

Stop at the first stage that does not match Patchright on detection.

## Stage 3 plan

`webskrap browser open` already starts Chromium itself with
`--remote-debugging-port`. Each later command is a new process that attaches,
acts on the current page, and detaches. Only the attach step and the page
operations change. The launch, profile, and session state stay as they are.

A new process cannot reattach to a pipe, so sessions use a WebSocket on the
loopback port. The browser already listens there today. The client is a small
RFC 6455 implementation on `asyncio` streams. It sends no `Origin` header, so
`--remote-allow-origins` stays unset.

The driver is chosen at `browser open --driver cdp` and stored in the session
state. Every later action uses that driver, because snapshot refs from one
driver do not resolve in the other. Patchright stays the default.

1. **3a: attach and page operations. Done.** WebSocket transport. Attach to the
   last page target. Navigation, history, reload, text, main-world `eval`,
   view, screenshot, key press, text insertion, mouse, and waits for text,
   CSS selectors, and load states. Raw Patchright calls in `browser_cli.py`
   and `mcp_server.py` move into `browser_session` functions, so call sites
   do not depend on the driver.
2. **3b: snapshot and element actions. Done.** Build the snapshot with a DOM walk
   in an isolated world, as Playwright's injected script does, and map each
   listed element to its `backendNodeId` with `DOM.describeNode`. Refs are
   `e<backendNodeId>`. They stay valid while the node lives and across
   connections, with no marker written into the page. Port the element
   actions, including the human cursor path and keystroke timing from
   `human.py`, to `Input.dispatch*` events.

   `Accessibility.getFullAXTree` was measured first and rejected. Once it
   runs, Chromium keeps its accessibility mode on: a 3000-button DOM burst
   took 136 to 145 ms instead of 122 to 128 ms (median of 5, 4 runs).
   `Accessibility.disable` did not restore it, and it sometimes survived a
   navigation. A page can time that, so the domain stays unused.
3. **3c: MCP and search. Done.** Add a `driver` option to the MCP `browser_open`
   tool and to `search`. Run `scripts/driver_parity.py` again with sessions.

Each step ends with tests, the full pipeline, and a commit. Measure
`Accessibility.getFullAXTree` for detection before 3b ships, because neither
Patchright nor Playwright uses that domain.

## v3 plan

v3.0.0 drives Chromium only through WebSkrap's own CDP client. Decisions:

- **Browser install.** `webskrap install` downloads Chrome for Testing, the
  build Google publishes for automation, for Linux (x64 and ARM64), macOS and
  Windows. Installed Chrome and Edge channels keep working.
- **Windows.** Windows launches with a loopback debugging port and attaches
  with the WebSocket client. Linux and macOS keep the debugging pipe.
- **Features.** Cookie-banner dismissal, proxy credentials, `storage_state`,
  and profile emulation (viewport, user agent, locale, timezone, media) move
  to CDP. The `firefox` and `webkit` browsers, the `driver` option and the
  `patchright_*` options go away.
- **Python API.** `WebSkrapClient`, its sessions, `fetch` and `search` keep
  their names and arguments. Playwright objects, such as
  `WebSkrapSession.context` and `Page` arguments, are replaced by CDP ones.

Phases, each ending with tests, the full pipeline and a commit. Phases 1 to 5
are done:

1. Launch: the Chrome for Testing installer, executable lookup, and the
   Windows port launch.
2. Client: `WebSkrapClient` and `WebSkrapSession` on CDP, with profile
   emulation, proxy credentials, `storage_state`, JavaScript and service
   worker switches, consent dismissal, and the human click and type helpers.
   Warm MCP fetch sessions run on it.
3. Sessions, CLI and MCP: CDP only; the driver options are removed.
4. Doctor and diagnostics on CDP.
5. Remove the dependencies, port the tests, and update the documentation,
   with a 2.x to 3.0 migration guide.
6. Parity runs, review, pull request, release.

## Design

`src/webskrap/cdp.py` is the core:

- **Transport.** On Linux and macOS, Chromium starts with
  `--remote-debugging-pipe`. It reads
  NUL-terminated JSON on file descriptor 3 and writes on descriptor 4.
  `os.posix_spawn` maps the descriptors. The browser opens no TCP port, so no
  other local process can attach. Windows cannot map the descriptors, so there
  Chromium opens a loopback debugging port and WebSkrap attaches with its own
  WebSocket client.
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
  suppression, `--disable-field-trial-config`, and
  `SessionConfig.browser_args()`. That includes
  `--disable-blink-features=AutomationControlled`, because no driver patch
  hides `navigator.webdriver`.

`src/webskrap/client.py` builds `WebSkrapClient` and `WebSkrapSession` on the
core. `src/webskrap/cdp_setup.py` prepares every page before it navigates:

- `Emulation` and `Network.setExtraHTTPHeaders`: the profile, as
  `SessionConfig.profile_mode` asks. Nothing is overridden by default.
- `Fetch`: only when a resource policy blocks resource types (then only for
  those types), or when proxy credentials are set. Credentials go only to
  challenges whose source is the proxy.
- `Storage.setCookies` and a throwaway page restore `storage_state` once per
  session.

## Measured results

These runs compared Patchright with the CDP driver in 2.15, before
Patchright was removed.

Host: Linux ARM64, bundled Chromium, sandbox off, same IP for both drivers.

Local probe page (`scripts/compare_drivers.py`, removed in 3.0). It checks `navigator.webdriver`,
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
| DataDome website (`--datadome-url`) | headless, virtual display | same page as CDP every run | same page as Patchright every run |

DataDome answered both drivers the same way on every run. Screenshots show
what that is on this datacenter (VPS) IP: an "access restricted" block page
in headless mode, and the slider captcha with a headed browser. Both come
from the same `/captcha/` iframe with `t=fe`, so the iframe URL cannot tell
them apart; an earlier version of the parity script counted every run as a
captcha and is corrected. The captcha is not solved.

Cloudflare needs the "Verify you are human" checkbox clicked; waiting alone
never clears it. `--click` moves both drivers along the same curved path
through CDP input events. The click clears the challenge in a headed
browser on a virtual display, but not in headless mode.

Persistent sessions (stage 3b), headless, 2 runs per driver: open, goto
deviceandbrowserinfo.com, snapshot, a human hover on a ref, then read the
verdict. Both drivers report the same: `isBot` (headless mode) and weak
signals, with no CDP, webdriver or Playwright flag.

Protected pages through persistent sessions (stage 3c,
`scripts/driver_parity.py --via session`), headless, 3 runs per driver; each
poll is a fresh attach, as with CLI commands and MCP tool calls:

| Target | Patchright | CDP |
| --- | --- | --- |
| Cloudflare Turnstile (peet.ws) | 3/3 token | 3/3 token |
| reCAPTCHA v3 demo | 3/3, score 0.9 | 3/3, score 0.9 |
| DataDome website | same page as CDP every run | same page as Patchright every run |
| Cloudflare challenge, no click | 0/3, challenge stays | 0/3, challenge stays |

Result: the CDP driver matches Patchright on every signal measured. It is
about 30 to 35 percent faster per cold fetch. It does not beat Patchright on
detection. The remaining failures come from the host (no GPU, headless mode),
not from the driver.

One difference was found and fixed. Chromium builds apply their testing field
trials by default, and one trial trims `navigator.languages` to one entry.
Playwright passes `--disable-field-trial-config`, so the CDP driver now does
the same.

## Limits

- One page per fetch. No auto-attach to workers, so a resource policy does not
  cover requests that they make.
- Selectors are CSS or snapshot refs. Playwright engines such as `text=` and
  `role=` do not work.
- `PLAYWRIGHT_BROWSERS_PATH` is still read, including `0` (package-local
  browsers), so a browser that 2.x downloaded is found.
- DuckDuckGo answers with a bot challenge from this host's IP; Bing serves
  results.
- The snapshot leaves out `generic` wrappers and Playwright's
  `[cursor=pointer]` marks. It does not descend into iframes; an iframe is
  listed as one node. Roles and names cover the ARIA implicit-role table and
  the common accessible-name rules, not the full specification.
- As in Playwright's strict mode, a CSS selector that matches several elements
  is refused; use a ref.
- A `networkidle` wait in a persistent session, on an already loaded page,
  means 500 ms with no request in flight that the command saw start and no
  new resource-timing entry, because the live lifecycle event fired before
  the command attached. A request that began earlier and still runs is
  invisible until it ends.
- Each command acts on the newest open page. CDP lists targets in no defined
  order, so the session directory keeps the creation order in
  `cdp-pages.json` (target ids only). Several pages opened while no command
  runs have no known order.
- `eval` runs with a user gesture, so it can open one popup per call. Its
  results use the 2.x shapes: `ref: <Window>`, `ref: <Node>`, and
  `[Circular]`.
- `CDPPage.evaluate` runs in an isolated world; use
  `browser_session.evaluate` for the page's own globals.

## Conditions for more work

- Parity held on Cloudflare and DataDome over 3 runs per mode (stage 2 gate
  met). On this datacenter host, neither driver clears the Cloudflare
  challenge pages without a click or in headless mode, and DataDome blocks
  headless runs and shows its captcha to headed ones. So stage 3 gains speed and
  control, not stealth. A residential IP or a GPU host is the next lever for
  detection.
- Proxy credentials pause every request through `Fetch`, so they cost a round
  trip per request. Measure before adding more request interception.
