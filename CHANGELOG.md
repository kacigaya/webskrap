# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries for releases before 0.9.1 are reconstructed from git tags and commit
history, so they summarize each release rather than list every change.

## [Unreleased]

### Added

- `blocked` now covers provider block pages: Cloudflare's own error pages
  (codes 1005-1010, 1012, 1015, 1020), site-styled Cloudflare 403 pages that
  show the response's Ray ID, and Akamai edge pages (HTTP 400 or 403 from
  `AkamaiGHost` with a reference number). Challenges now also include Akamai's
  sec-cpt page and the AWS WAF challenge and CAPTCHA actions.
- `blocked` errors carry a kind-specific hint: change the exit IP for a
  block, try headed mode or a persistent profile for a challenge. On Linux
  with Xvfb, both name `virtual_display=True` first. `WebSkrapError.hint`
  holds it for Python callers.
- `decline_cookies` for MCP `browser_open` and `browser_goto`, and
  `--decline-cookies` for `webskrap browser open` and `goto`. The result
  reports `cookie_notice_declined`, as fetches do.
- `browser_goto` (and `browser_open` with a URL) reports a challenge or block
  page in a new `blocked` key, without raising.
- Element screenshots: `target` for MCP `browser_screenshot` and `--target`
  for `webskrap browser screenshot` take a selector or snapshot ref, iframe
  refs included, and capture only that element. An element inside an iframe
  is cut to the iframe's content box. MCP output stays confined to
  `./webskrap-output`.
- `browser_text` takes `pattern`, `context` and `max_matches` to return only
  matching lines. The regex runs in a child Python process that is killed
  after 2 seconds, so a backtracking pattern cannot hang the server.

### Fixed

- A page that reloads itself before reaching the requested load state (for
  example, behind a Cloudflare JS check under `virtual_display`) no longer
  times out. Navigation follows the replacement document and reports its
  response.
- Self-clearing interstitials (Akamai sec-cpt, AWS WAF challenge) are waited
  out for up to 15 seconds. Fetches used to read the interstitial and could
  report it as `ok: true` with HTTP 202.
- `search` runs the same provider challenge check as `fetch`, including the
  interstitial wait.
- Closing a tab waits until Chromium stops listing it. The next browser
  command could otherwise attach to the closed tab and fail with "No target
  with given id".

## [3.1.0] - 2026-10-26

### Added

- Snapshots walk into iframes, same-site and cross-site. Frame content is
  listed under its `iframe` line. Its refs look like `f12345e15` and work with
  every element action and `wait_for`.
- Persistent sessions can use a proxy that asks for a password. Set
  `WEBSKRAP_PROXY_USERNAME` and `WEBSKRAP_PROXY_PASSWORD` and run
  `webskrap browser open --proxy http://...`, or pass `proxy_username` and
  `proxy_password` to `open_session`. A helper process keeps the credentials
  in memory and answers the proxy while the browser runs. They are never
  written to disk or put on a command line. `browser list` marks these
  sessions with `(auth)`.
- Tabs in persistent sessions: `webskrap browser tab-list`, `tab-new [url]`,
  `tab-select <index>` and `tab-close [index]`, and the MCP tool
  `browser_tabs`. Other commands act on the current tab. A popup or a new tab
  becomes current, as before.

### Fixed

- Humanized clicks retry an empty bounding box after scrolling and check the
  target again after moving the pointer. Hover handlers that move, hide,
  disable, or cover the target can no longer cause a click at an unsafe point.
- Humanized pointer paths end at the target point, including for tiny hover
  targets. Click offsets now reach up to 12 CSS pixels horizontally and 10
  vertically, with smaller offsets for small targets. Clicks pause for
  200–500 ms before pressing.
- With `profile_mode="full"`, a profile user agent now comes with matching
  client hints: `navigator.userAgentData` and the `Sec-CH-UA` headers. Before,
  the brands and platform were empty and no `Sec-CH-UA` header was sent,
  which no real Chrome does.
- `profile_mode` `locale` and `full` now apply to cross-site iframes and to
  workers. Before, with `full`, a cross-site iframe reported the browser's own
  user agent, timezone and languages. Iframes still report the real screen
  size and pixel ratio, because Chromium allows that override on top-level
  pages only.
- Resource policies now block images, fonts and media in cross-site iframes
  and in shared and service workers.

## [3.0.1] - 2026-10-25

### Changed

- `scripts/driver_parity.py` reads DataDome's `/captcha/` iframe and reports
  `captcha` (the slider) or `blocked` (the hard block) instead of a neutral
  `datadome` verdict. Persistent-session runs (`--via session`) still report
  `datadome`.
- The benchmarks page shows measurements of 3.0.0 with Chrome for Testing 156.

### Fixed

- The live WebRTC test read the launch flags through the removed
  `launch_options()` and failed before it checked the page.
- A test now covers the `goto()` document-response deadline fixed in 3.0.0.

### Security

- Hide complete URL credentials in errors, including multiple `@` characters
  and malformed inputs with `?` or `#` before `@`.

## [3.0.0] - 2026-10-25

WebSkrap 3.0 drives Chromium only through its own CDP client. Read
`docs/migration-3.0.md` before you upgrade.

### Added

- `webskrap install` downloads Chrome for Testing (Linux x64 and ARM64, macOS,
  Windows) into the user cache folder. `--force` downloads it again.
  `WEBSKRAP_BROWSERS_PATH` changes the folder.
- Windows support. WebSkrap connects through a loopback debugging port and its
  own WebSocket client there; Linux and macOS keep the pipe.
- `SessionConfig.profile_mode`: `native` (default), `locale` or `full`. The
  CLI takes `--profile-mode` and the MCP fetch tools take `profile_mode`.
- Consent dismissal, proxy credentials, `storage_state`, resource policies and
  profile emulation work on the CDP client. Proxy credentials go only to
  authentication challenges from the proxy, never to a website.

### Changed

- `WebSkrapSession` owns a `CDPBrowser` (`session.browser`).
  `session.new_page()` returns a `CDPPage`.
- Selectors are CSS or snapshot refs everywhere.
- The browser lookup prefers Chrome for Testing, then a browser that 2.x
  downloaded with Playwright, then a distribution Chromium.
- `scripts/driver_parity.py` compares the client path with the bare CDP core.
- Headless launches declare a mouse with `--blink-settings`, as Playwright
  did, so pages see hover and a fine pointer.
- A consent click may take up to 4 s, and the work after a notice is found
  up to 5 s. The humanized click was cut short on slow hosts.
- `ProxyConfig.server` refuses `user:password@`. Set `username` and
  `password` instead, so the password never reaches Chromium's command line.

### Removed

- The `playwright` and `patchright` dependencies.
- `SessionConfig` fields `driver`, `browser`, `slow_mo_ms`,
  `patchright_focus_control` and `patchright_context_profile`. Setting one
  raises an error that names its replacement.
- The `--driver` and `--patchright-context-profile` options, and the MCP
  `driver` and `patchright_context_profile` arguments.
- `webskrap.cdp_fetch`, `WebSkrapClient.start()`, `WebSkrapSession.context`
  and the Playwright helpers in `webskrap.human`.
- `scripts/compare_drivers.py`.

## [2.15.0] - 2026-10-24

### Added

- Experimental CDP driver: `webskrap fetch --driver cdp` and
  `webskrap.cdp_fetch.fetch()`. WebSkrap drives Chromium over its debugging
  pipe with no Playwright, Patchright, or Node.js process. It never sends
  `Runtime.enable` and runs page scripts in isolated worlds. Linux and macOS
  only. See `docs/design/cdp-driver.md` for limits and measured results.
- `scripts/compare_drivers.py` compares automation signals and fetch time of
  the Patchright and CDP drivers on a local probe page.
- `webskrap browser open --driver cdp`: persistent sessions on the CDP driver.
  Later commands attach over a built-in WebSocket client. Every browser
  command works, including `snapshot` with `eN` refs and the element actions
  with human cursor paths and keystroke timing. Snapshots come from a DOM
  walk in an isolated world; the Accessibility domain is not used, because
  it leaves a measurable slowdown on the page.
- `webskrap search --driver cdp`, and a `driver` argument on the MCP
  `stealth_fetch`, `search` and `browser_open` tools. `decline_cookies`
  defaults to on for Patchright only; asking for it on `cdp` is refused.
- `scripts/driver_parity.py` runs both drivers on Cloudflare, Turnstile,
  reCAPTCHA v3 and DataDome test pages, with screenshots, an opt-in
  Turnstile checkbox click, and `--via session` to go through persistent
  sessions.

### Changed

- `raise_for_challenge` accepts any object with an `evaluate` method, so both
  drivers share one challenge check.
- Session state records the driver. `browser open` refuses to reuse a running
  session with a different driver, because snapshot refs do not carry over.

## [2.14.0] - 2026-10-23

### Added

- Agent skill folders under `.agents/skills/webskrap`, `.claude/skills/webskrap`,
  and `.codex/skills/webskrap`. Each links to the shared skill and references.
- `doctor` warns when the browser is bare Chromium. Its `Sec-CH-UA` header
  lists only `Chromium` and a GREASE brand, with no `Google Chrome`, while the
  user agent says `Chrome`. Some detectors score this as a weak bot signal.
  Real Chromium users send the same header, so WebSkrap does not fake the brand.

### Changed

- `SKILL.md` moved from the repository root to `.agents/skills/webskrap/SKILL.md`.
  The `.claude` and `.codex` skill folders link to it. Update any tool that
  loads the skill from the root path.

### Fixed

- Persistent sessions no longer work on a blank tab after `browser open`.
  Chromium could report its DevTools port before it opened the startup tab,
  most often on Windows. `open` then created a second page, and later commands
  could pick the blank one, so waits timed out and snapshots came back empty.
  Launch now waits for the startup tab.

## [2.13.3] - 2026-10-22

### Fixed

- 2.13.2 did not reach PyPI: a new session test failed by chance on Linux CI
  and stopped the publish job. This release has the same changes as 2.13.2,
  and the test now waits for the child process before it checks it.

## [2.13.2] - 2026-10-22

### Fixed

- Persistent browser sessions now close on Windows. `os.killpg` and
  `signal.SIGKILL` do not exist there, so `browser close` and launch cleanup
  raised `AttributeError`. Windows now stops the browser tree with `taskkill`.
  The liveness check read `/proc`, so a running session always looked closed.
  It now asks Windows whether the PID is alive and uses its start time to reject
  a recycled PID.
- macOS has no `/proc` either, so a running session also looked closed there.
  Reopening it launched a second browser on the locked profile and failed. The
  check now reads the command line from `ps` when `/proc` is missing.
- CLI output no longer splits a long file path across two lines.

### Security

- Update the documentation site's Next.js and matching ESLint configuration to
  16.4.2, and pin `sharp` 0.35.5, `source-map-js` 1.2.2, and
  `postcss-selector-parser` 7.1.6. This fixes the Next.js, librsvg, and
  denial-of-service advisories that failed the Pages dependency audit.

## [2.13.1] - 2026-10-14

### Fixed

- MCP `fetch`, `stealth_fetch`, and `search` fall back to Edge, then bundled
  Chromium, when the default `chrome` channel cannot launch, matching the CLI.
  On Linux ARM64 these tools failed with `browser_launch` on every call unless
  the caller passed `channel="chromium"`. The working channel is remembered for
  the process, so warm fetch sessions keep a stable config.

## [2.13.0] - 2026-10-01

### Added

- Python fetches, CLI `fetch --ready-selector`, and MCP fetch tools can wait for
  a visible Playwright selector before extracting content. Readiness uses a
  separate navigation-timeout budget after consent dismissal.

### Fixed

- Known DataDome and Cloudflare challenge pages raise `blocked` instead of
  returning challenge content, including challenges served with HTTP 200.
  Fetches check before consent, before extraction, and after a readiness
  timeout. Ordinary HTTP error pages still return a result with `ok=False`.
- Live stealth checks fail known challenge pages before skipping service errors.
- Sandbox recovery hints include executable-specific AppArmor policy checks.

### Migration

- Callers that previously inspected raw challenge content from a fetch should
  handle `WebSkrapError` with `code=ErrorCode.BLOCKED`. Use a manually driven
  session page for diagnostics; fetches no longer return recognized challenges.

## [2.12.0] - 2026-09-30

### Security

- Update the documentation site's Next.js and matching ESLint configuration to
  16.3.6, fixing GHSA-vcvr-r3jv-pc5j in `next/og` and restoring the Pages
  dependency audit.

### Added

- MCP `browser_view` returns an inline viewport PNG with CSS coordinate metadata,
  without writing a file. CLI `browser view` saves the same image and reports
  its absolute path and dimensions in JSON.
- MCP `browser_mouse` and CLI `browser mouse` support coordinate clicks,
  double-clicks, movement, scrolling, and dragging. Coordinates must be finite
  and inside the viewport; drag releases the button even when movement fails.
- MCP `browser_insert_text` and CLI `browser insert-text` insert literal Unicode
  into the focused control. The skill and guides explain the visual interaction
  loop, image-client requirements, and coordinate mapping.

### Fixed

- Expected MCP failures retain their error code and recovery hint over the
  protocol instead of being hidden as unexpected SDK exceptions.

## [2.11.0] - 2026-09-29

### Added

- MCP `browser_text` returns visible text from an open session in pages,
  with length, truncation, and continuation metadata, without building an
  accessibility tree.
- MCP `fetch` and `stealth_fetch` accept an opt-in `session` name for warm
  browser reuse. `fetch_session_close` releases idle fetch sessions. Cookies
  and storage are shared only when requested; eight fetches may run at once
  and eight named sessions may stay open.
- `stealth_fetch` accepts `resource_policy` and `wait_until`, retaining its
  `all` and `domcontentloaded` defaults.
- `WebSkrapClient.close_session(name)` closes and forgets a named session.
- Python fetches accept a per-call `decline_cookies` override. Warm MCP
  sessions can vary consent dismissal and navigation timeout without relaunching.
- Benchmarks cover the MCP fetch runtime, native headless mode, UA masking,
  and configurable repeat, warmup, and concurrency counts.

### Changed

- MCP `fetch`, `stealth_fetch`, `browser_snapshot`, and `browser_eval` default
  `max_chars` to 8000 instead of 20000. Larger explicit limits still work.
- MCP `browser_snapshot` defaults to depth 6. Pass `depth: null` for the full
  tree. The CLI and Python snapshot defaults are unchanged.
- Link extraction skips labels past `max_links` while keeping URL order,
  deduplication, and the total unique link count.
- MCP fetch tools retain their driver for the server lifetime. Unnamed calls
  still use fresh browsers and temporary profiles.
- UA masking caches successful probes per browser/channel/sandbox setting
  within a client and removes the two-second delay after probe shutdown.
- Temporary browser profile cleanup runs off the event loop.

### Fixed

- Cancelling a caller waiting for a shared session launch no longer cancels
  other callers or loses ownership of the launching browser.
- MCP cancellation completes page and browser cleanup. Closing one fetch
  session no longer holds the registry lock during browser teardown.

## [2.10.1] - 2026-09-29

### Added

- `benchmarks.py --mode headed` measures `headless=False` on the current
  display, next to the default virtual-display mode. Without a display on
  Linux it exits and suggests `xvfb-run -a`.
- Live stealth suites read `WEBSKRAP_LIVE_GPU=mesa` to render WebGL through
  Mesa lavapipe on hosts without a GPU. The live report records the GPU
  backend and the virtual-display mode.

### Changed

- Live stealth tests skip a demo site that answers with a 5xx status instead
  of failing. A Cloudflare 503 challenge still fails. Live suites honor
  `WEBSKRAP_CHROMIUM_SANDBOX=0` and `WEBSKRAP_LIVE_VIRTUAL_DISPLAY=1`.
- The benchmarks page publishes the September 29 stealth results and a headed
  vs virtual-display timing comparison.

### Security

- The lockfile pins PyJWT 2.15.1, pulled in by `mcp[crypto]`, which fixes
  CVE-2026-102274.

## [2.10.0] - 2026-09-25

### Added

- `doctor` reports the launched browser identity, host CPU architecture, and
  distinct Fontconfig family count. The font count is `null` when `fc-list`
  is unavailable.

### Changed

- `doctor`, `fetch`, and `search` try installed Edge after Chrome fails to
  launch, then bundled Chromium. Sandbox failures still stop immediately in
  `fetch` and `search`.
- Browser profiles leave `Accept` to the browser instead of injecting a fixed
  value. Caller-supplied `Accept` headers still apply.
- Benchmark documentation notes that timings vary by host and browser version;
  the benchmark script honors `WEBSKRAP_CHROMIUM_SANDBOX`.

## [2.9.0] - 2026-09-25

### Added

- `SessionConfig(fake_media_devices=True)` and `--fake-media-devices` on fetch,
  search, and browser open enable Chromium's synthetic camera and microphone.
  MCP `stealth_fetch`, `search`, and `browser_open` offer the same option.
  Permission prompts remain active. Persistent sessions need reopening to
  enable it.

### Changed

- Headless Chromium windows leave 80 pixels free at the right and bottom of
  the configured screen instead of filling it. The headless screen reports
  80 pixels less available height for a taskbar. Browser launches omit
  Playwright's headless switches that hide scrollbars and mute audio.

## [2.8.0] - 2026-09-25

### Added

- `WebSkrapSession.human_type` and `webskrap.human.type_text`: click into a
  field and type one key at a time, each held 30-90 ms and 40-160 ms apart,
  with an occasional longer pause.
- Persistent browser sessions can run behind a proxy:
  `webskrap browser open --proxy URL` and `open_session(proxy_server=...)`.
  `--webrtc-ip-handling-policy` (CLI) and `webrtc_ip_handling_policy`
  (`open_session`, MCP `browser_open`) set the WebRTC policy. Both are fixed at
  launch; reopening a running session with a different proxy or policy is a
  `usage` error. Proxy URLs with credentials are refused, since nothing stays
  attached to answer the proxy. `browser list` shows each session's proxy. The
  MCP tools take no proxy argument.

### Changed

- `human_click` scrolls with mouse-wheel notches instead of
  `scroll_into_view_if_needed` (which fires no `wheel` events), holds the
  button 60-140 ms unless `delay` is given (Playwright holds it ~2 ms), and
  refuses a target covered at the click point, checked with `elementFromPoint`
  before any pointer event.
- Cookie consent dismissal clicks the reject control with the humanized click,
  including inside CMP iframes.
- Persistent-session `click`/`dblclick` (`webskrap browser`, MCP
  `browser_interact`) use the humanized click, and `type` uses human typing.
  They are slower than before by design; `fill` is unchanged.
- The humanized input moved to `webskrap.human`, which takes a locator and
  either driver's page.
- A session with a proxy and no explicit `webrtc_ip_handling_policy` now gets
  `disable_non_proxied_udp`, so WebRTC no longer reveals the host's LAN and
  direct public addresses behind the proxy. Pass `"default"` to keep
  Chromium's behavior.

## [2.7.0] - 2026-09-24

### Added

- `SessionConfig(gpu="mesa")`, `webskrap fetch/search --gpu mesa` and the MCP
  `gpu` parameter render WebGL through Mesa's software Vulkan driver
  (lavapipe) instead of SwiftShader, whose renderer string real Chrome stopped
  reporting in Chrome 139 and detectors read as automation. Linux only, needs
  `mesa-vulkan-drivers`; a missing driver fails the session before launch.
  `gpu="auto"` (the default) keeps Chromium's choice. `doctor` reports
  `mesa_gpu_available`.

### Security

- One-shot `fetch`/`search` sessions now actually keep Chromium's OS sandbox.
  Since 2.3.0 they only stopped *adding* `--no-sandbox`, but never passed
  `chromium_sandbox=True` to Playwright, which then appended `--no-sandbox`
  itself, so every one-shot launch ran unsandboxed. `doctor` now probes with
  the same setting fetches use. Hosts that cannot sandbox (unprivileged user
  namespaces off, root in a container) now fail with a `sandbox` error instead
  of silently running without isolation; opt out explicitly with
  `--no-sandbox`, `SessionConfig(chromium_sandbox=False)`, or
  `WEBSKRAP_CHROMIUM_SANDBOX=0`. Such failures are no longer reported as
  `browser_launch` with an install hint, and the CLI no longer retries them on
  another channel.

## [2.6.0] - 2026-09-24

### Fixed

- Headless Chromium runs with no `channel` now use the full browser in new
  headless mode (`channel="chromium"`) instead of Playwright's headless shell,
  which sent `HeadlessChrome` in `Sec-CH-UA` even with the user agent masked,
  omitted `Accept-Language`, and exposed no PDF plugins or `window.chrome`.
  `channel="chromium-headless-shell"` opts back in. The `fetch`/`search`
  fallback and `doctor` now try `chromium` instead of the shell.
  This needs the full Chromium download, which `webskrap install` fetches;
  an install made with `playwright install --only-shell` must re-run it.
- With `patchright_context_profile` on Linux, the profile's timezone
  and languages are applied natively (`TZ`, `LC_ALL`/`LANG`/`LANGUAGE`,
  `--accept-lang`) instead of through CDP overrides, so `navigator.languages`
  carries the whole profile list and `Accept-Language` gets Chromium's own
  q-values. Unknown timezones are rejected before launch instead of surfacing
  as `Etc/Unknown`.

### Added

- `doctor` reports `host_timezone` and a warning when it is UTC.

## [2.5.0] - 2026-09-23

### Added

- `SessionConfig(virtual_display=True)`, `webskrap fetch/search
  --virtual-display` and the MCP `virtual_display` parameter run a headless
  session as headed Chromium on a private Xvfb display (Linux). The page sees
  no `HeadlessChrome` token, full user-agent client hints, and real scrollbars,
  with nothing overridden. Each session gets its own Xvfb, reachable only with
  a random cookie, stopped when the session closes or startup is cancelled.

### Fixed

- Documented that `mask_headless_user_agent` empties the high-entropy client
  hints (Chromium drops them when `--user-agent` is set); the docs had claimed
  they were left intact. The option is ignored when `virtual_display` is set.

## [2.4.0] - 2026-09-23

### Fixed

- Persistent browser sessions (`webskrap browser`, MCP `browser_*`) no longer
  expose automation. Actions attach through Patchright instead of Playwright,
  so the CDP `Runtime.enable` leak no longer reaches pages or workers, and the
  browser launches with `--disable-blink-features=AutomationControlled` and the
  headless virtual screen, so `navigator.webdriver` is false and the
  800x600 headless screen is gone. `browser eval` evaluates in the page's own
  world so page globals stay visible. Sessions already running keep their old
  launch flags until closed and reopened.

## [2.3.0] - 2026-09-18

### Security

- One-shot `fetch`/`search` calls now keep Chromium's OS sandbox by default,
  matching persistent sessions. Opt out per call with `webskrap fetch
  --no-sandbox`, `webskrap search --no-sandbox`, or
  `SessionConfig(chromium_sandbox=False)`, or per host with
  `WEBSKRAP_CHROMIUM_SANDBOX=0`. Sandbox-weakening flags (`--no-sandbox`,
  `--disable-setuid-sandbox`, `--load-extension`, `--unsafely-*`,
  `--remote-debugging-*`) are rejected when passed through `--launch-arg`.
- Fetch and navigation targets must be `http`/`https` URLs without embedded
  credentials (`data:` pages and `about:blank` stay allowed). The MCP server
  additionally refuses hosts resolving to non-public addresses unless
  `WEBSKRAP_ALLOW_PRIVATE_NET=1` is set.
- MCP `browser_eval` caps expressions at 10,000 characters, logs them
  server-side, and can be disabled with `WEBSKRAP_ALLOW_EVAL=0`.
- Proxy credentials are redacted from `ProxyConfig` display output, embedded
  URL credentials are scrubbed from error text, and shaped CLI/MCP fetch
  payloads return an allowlist of response headers (no `set-cookie`).
- MCP output and profile directories are created level-by-level `0700`,
  symlinks along the way are rejected, and destinations are re-resolved
  before use to narrow the resolve-to-write race.

## [2.2.0] - 2026-09-15

### Added

- Privacy and cookie policy pages on the documentation site, linked from the
  site footer.

### Changed

- The MCP server runs on the `mcp` 2.x SDK (`MCPServer`, formerly
  `FastMCP`). Installs now require `mcp>=2`; the wire protocol, tool names,
  and tool annotations are unchanged. The server also reports the installed
  WebSkrap version in `serverInfo`, where the 2.x SDK would otherwise send an
  empty string.

## [2.1.1] - 2026-09-11

### Changed

- Bing is the default search engine for the Python API, CLI, and MCP tool
  because DuckDuckGo frequently serves bot challenges. DuckDuckGo remains
  available through `engine="ddg"` or `--engine ddg`.

## [2.1.0] - 2026-09-11

### Added

- Web search. `client.search()` and `session.search()` load DuckDuckGo's
  HTML results page (the default) or Bing's in the same session a fetch
  uses, so proxy, consent and persistent-profile settings apply, then parse
  the organic hits in Python. Each engine's click-tracking redirect is
  unwrapped and duplicate destinations are dropped. No new dependencies.
  Google is not offered. New exports: `SearchEngine`, `SearchHit`,
  `SearchResult`.
- `webskrap search "query" --engine ddg --max-results 10`, with `--format
  json` and the stealth options `fetch` has.
- MCP tool `search`, with the `stealth_fetch` controls plus `engine` and
  `max_results`. The server instructions and guide used to say there was no
  search tool; they now route "find URLs" to it.
- `ErrorCode.BLOCKED` (exit 11) for a site that answers with a bot challenge
  instead of content. A search raises it instead of returning zero hits.
  Nothing is retried and no CAPTCHA is solved.

## [2.0.1] - 2026-08-30

### Changed

- Reworked the WebSkrap skill around task routing, shared-code boundaries, and
  public-contract checks. Detailed Python, CLI, and MCP guidance now lives in
  focused references that agents load only when needed.
- Updated documentation tests to verify skill routing and source-of-truth links
  without duplicating dynamic command, tool, environment, and error catalogs.

## [2.0.0] - 2026-08-30

Everything in this release is aimed at one problem: an agent driving WebSkrap
had to learn the tool surface by making calls that failed. Failures said what
broke but not what to do; truncated results were dead ends; and the MCP server
shipped a dozen tool descriptions with no statement of how they fit together.

### Added

- `webskrap.errors`: an `ErrorCode` for every failure, a fixed recovery hint per
  code, and a CLI exit status per code. Playwright's own exceptions are
  classified through the same table, so a caller does not need to know which
  layer raised.
- Offset paging for anything that gets truncated. Fetch payloads carry
  `text_offset` and `next_text_offset`, snapshots carry `snapshot_offset` and
  `next_snapshot_offset`, and both accept an `offset` to resume from. A clipped
  result no longer has to be re-fetched with a larger limit.
- Bounded link extraction (`include_links`, `max_links`, `FetchResult.links`,
  `links_total`). Hrefs resolve against the final URL; duplicates and
  `javascript:` targets are dropped. Off by default.
- A wait primitive: `browser_wait_for` (MCP) and `webskrap browser wait` (CLI),
  waiting on visible text, absent text, a selector reaching a state, or a load
  state. Exactly one condition is accepted.
- MCP server `instructions` stating which tool to use for which goal, the
  session limits, the cost levers, and that there is no search tool.
- MCP tool titles and readOnly/destructive/idempotent/openWorld annotations.
  Interaction tools are marked destructive because a click or an Enter submits
  forms on sites WebSkrap does not own.
- MCP resources `webskrap://guide`, `webskrap://profiles` and
  `webskrap://sessions`, so static facts cost no tool call.
- `webskrap schema`, describing every command, option, type, choice and default
  as one JSON document.
- `webskrap.diagnostics`: `doctor` now also reports installed versions, the
  Chromium binary in use, the confined roots, which `WEBSKRAP_*` overrides are
  set, and every session with its running state.

### Changed

- **Breaking.** CLI failures exit with a status per error kind (2 usage,
  3 timeout, 4 navigation, 5 browser launch, 6 sandbox, 7 no session,
  8 unreachable session, 9 stale ref, 10 rejected path) instead of always 1. A
  command that ran to completion and reports a negative result -- `doctor` on a
  host with no browser -- still exits 1.
- **Breaking.** CLI failures under `--format json` print
  `{"ok": false, "error", "code", "hint"}` to stdout instead of Rich markup on
  stderr, so a parser gets a parseable answer whether or not the command worked.
- **Breaking.** MCP tool failures carry their code and recovery hint in the
  message rather than only the first line of the underlying exception.
- **Breaking.** Fetch payloads gain `text_offset`, `next_text_offset`, `links`,
  `links_total` and `links_truncated`.
- **Breaking.** `browser_eval` and `webskrap browser eval` bound their result:
  past `max_chars` the value is returned as clipped JSON in `result_json` with
  `result_truncated` set, so one `document.body.innerHTML` cannot flood a
  caller.
- **Breaking.** `browser_snapshot` payloads gain `snapshot_length`,
  `snapshot_offset` and `next_snapshot_offset`.
- **Breaking.** `browser_doctor` reports `driver`, `channel` and
  `executable_path`.
- `WebSkrapError` moved to `webskrap.errors` and accepts an optional code. It is
  still exported from `webskrap` and still importable from `webskrap.client`.
- `webskrap browser snapshot` accepts `--max-chars` and `--offset`, matching the
  MCP tool it had drifted from.
- SKILL.md is organized around the questions an agent arrives with: which entry
  point, what comes back, how to keep it small, and what to do about each error.

## [1.0.2] - 2026-08-29

### Changed

- Update development and documentation-site dependencies. The published
  package is unchanged from 1.0.1.

## [1.0.1] - 2026-08-29

### Security

- Persistent browser session names `.` and `..` are rejected. They previously
  resolved to the session root or its parent, so closing one with profile-data
  deletion enabled could remove files outside an individual session directory.
- Persistent browser commands reject a session directory that is a symlink,
  preventing a preplanted entry under a shared `WEBSKRAP_BROWSER_DIR` from
  redirecting profile and state-file operations outside the sessions root.
- MCP `stealth_fetch` profile paths are confined under
  `~/.webskrap/profiles` (override with `WEBSKRAP_MCP_PROFILE_DIR`). Absolute
  paths, traversal, and escaping symlinks are rejected before Chromium starts;
  unrestricted profile paths remain available through the Python API.

### Fixed

- Concurrent open and close operations for the same persistent browser session
  are serialized across processes. This prevents duplicate Chromium launches,
  stale state files, and a close racing with an in-progress open.
- `webskrap fetch --text-only --output` labels the saved artifact as text in
  its human summary instead of calling it HTML.

### Added

- Python 3.14 support and CI coverage.
- Dependabot configuration for the Python, GitHub Actions, and docs-site
  dependency trees, grouped so routine minor and patch bumps arrive as one
  weekly pull request and majors stay separate. `pip-audit` already failed CI
  on a known vulnerability; this is what opens the pull request that fixes it.

## [1.0.0] - 2026-08-22

First stable release. The public API is what `webskrap.__all__` exports plus
the documented CLI and MCP tool surfaces; from here breaking changes to those
require a major version. Nothing in the library changed from 0.10.1 beyond the
entries below.

### Security

- Permission tightening no longer follows symlinks. `secure_directory` used
  `Path.chmod`, which resolves the final component, so a symlink planted where
  a session directory belongs could have handed the mode change to its target.
  It now opens the directory with `O_NOFOLLOW` and uses `fchmod`, leaving a
  symlinked directory and its target untouched.
- The MCP output root is created `0700` when WebSkrap creates it, so no other
  local account can plant symlinks inside the default `./webskrap-output`. A
  root the user set up keeps its own permissions.
- Cursor jitter draws from `secrets.SystemRandom` instead of `random`. The
  values are pixel offsets and sleep durations, never secrets, but the change
  removes the last non-cryptographic RNG from the package, so no security
  scanner has to be told to ignore one.

### Added

- Bandit runs as an independent security gate (`bandit -c pyproject.toml -r
  src/`), in CI alongside Ruff's `S` rules. It is a second implementation of
  the same checks and reads the `# nosec` markers Ruff ignores. Every finding
  is suppressed at its own line with a reason; nothing is skipped globally, so
  the same rule still catches an unsafe use elsewhere.
- A dedicated `coverage` CI job that combines the data files from every Python
  version and the browser job, then enforces the 85% bar over the union. It
  depends on all test jobs, so the gate cannot pass on a partial run. Branch
  coverage is on.

### Changed

- Ruff's `D105` and `D107` are enforced: `D203` and `D213` are now the only
  ignored docstring rules. The affected magic methods and constructors describe
  lazy driver start, context ownership, and what leaving an `async with` block
  releases.
- CI separates its two requirements: Python 3.11/3.12/3.13 compatibility, and
  full-library coverage. Browsers install in one job rather than in every
  matrix entry.

### Fixed

- `resolve_output_path` rejects destinations that name no file (`.`, `""`)
  with a clear message rather than failing later inside the browser.

## [0.10.1] - 2026-08-22

### Fixed

- CLI usage-error tests asserted against Rich's wrapped panel output, so they
  passed locally and failed on CI's narrower terminal. Library behavior is
  unchanged; 0.10.0 never reached PyPI because the release workflow is gated
  on those tests.

## [0.10.0] - 2026-08-22

### Security

- Persistent browser sessions now keep Chromium's OS sandbox. Previously every
  session launched with `--no-sandbox`, so a renderer compromised by a hostile
  page had no containment. Opt out with `webskrap browser open --no-sandbox`,
  the `chromium_sandbox=False` argument to `open_session`/`launch_browser`, or
  `WEBSKRAP_CHROMIUM_SANDBOX=0` where the sandbox genuinely cannot start. A
  sandbox-related launch failure reports how to opt out; it never retries
  without the sandbox on its own.
- MCP screenshots are confined to an output root (`./webskrap-output` by
  default, `WEBSKRAP_OUTPUT_DIR` to move it). `browser_screenshot` previously
  wrote to any path a model asked for and created parent directories along the
  way. Absolute paths, `..` traversal, and symlinks leaving the root are now
  rejected before the browser is touched.
- Session directories (each session and its profile data) are created `0700`
  on POSIX and tightened if they already exist. They hold cookies and
  logged-in state that other local accounts could read. A sessions root the
  user set up themselves (`WEBSKRAP_BROWSER_DIR`) keeps its permissions, since
  it may be shared deliberately; WebSkrap only creates its own root `0700`.

### Added

- `webskrap.paths` with `resolve_output_path` and `secure_directory`.
- `browser_session.sandbox_enabled` and `create_session_dir`.
- `--no-sandbox` on `webskrap browser open`, plus a warning when a session
  starts without the sandbox.
- `chromium_sandbox` in a session's state file, in `browser open` output, and
  as a `Sandbox` column in `webskrap browser list` / `browser_list`, so an
  operator can see which running sessions gave up renderer isolation.
- `CHANGELOG.md`, `SECURITY.md`, and `CONTRIBUTING.md`.

### Changed

- Docstrings across the public API: client, sessions, models, parsing,
  profiles, and CLI commands.
- Ruff now enforces `S` (security) and `D` (docstring) rules, with narrow
  per-file exceptions instead of blanket ignores. Pyright's
  `typeCheckingMode` is pinned to `standard` in `pyproject.toml`.
- CI runs the suite on Python 3.11, 3.12 and 3.13, gates coverage at 85%, and
  checks package metadata with `twine check` before any PyPI upload.
- `pytest-cov` is part of the `dev` extra.

### Migration

- Sessions on hosts without a working Chromium sandbox (unprivileged
  containers, images with user namespaces disabled) now fail to open until you
  opt out with `--no-sandbox` or `WEBSKRAP_CHROMIUM_SANDBOX=0`. The error names
  both.
- MCP clients passing absolute screenshot paths must switch to relative ones,
  or set `WEBSKRAP_OUTPUT_DIR` to the directory they want written to.
- `webskrap browser screenshot` (the CLI) is unchanged: its path comes from the
  person running the command, not from a model.

## [0.9.1] - 2026-08-20

### Changed

- Trimmed the skill description and tightened prose across `SKILL.md` and the
  docs site.

## [0.9.0] - 2026-08-20

### Added

- Persistent browser sessions for the CLI (`webskrap browser ...`) and the MCP
  server: a detached Chromium that outlives each invocation, driven over CDP
  with aria-snapshot element refs.

### Fixed

- Overrode `nanoid` and `js-yaml` to patched versions in the docs site.

## [0.8.0] - 2026-07-30

### Changed

- Safer browser defaults.

### Added

- Mobile docs menu on the docs site.

### Fixed

- Kept the mobile docs menu inside the viewport.
- Pinned `setup-uv` to v9.0.0; publishing is gated on lint, tests and build.

## [0.7.1] - 2026-07-27

### Fixed

- `webskrap fetch` falls back to bundled Chromium when the requested channel
  cannot launch, and cookie-consent waits are capped.

## [0.7.0] - 2026-07-27

### Changed

- Relicensed to Apache-2.0.

### Added

- Automatic cookie-consent declining.

## [0.6.1] - 2026-07-01

### Removed

- Dead code and unused dependencies.

## [0.6.0] - 2026-07-01

### Changed

- Synced docs-site dependencies.

## [0.5.9] - 2026-07-01

### Changed

- The MCP `fetch` tool uses the stealth driver by default.

## [0.5.8] - 2026-07-01

### Changed

- MCP tools return clean page text by default, for LLM consumers.

## [0.5.7] - 2026-07-01

### Added

- Daily PyPI update-check notice in the CLI.

## [0.5.6] - 2026-07-01

### Added

- Benchmarks docs page and Patchright focus control.

## [0.5.5] - 2026-06-30

### Fixed

- Removed an unsupported Patchright focus option.

## [0.5.4] - 2026-06-30

### Added

- `webskrap install` covering both Playwright and Patchright browsers.

## [0.5.3] - 2026-06-30

### Added

- LLM-friendly CLI output.

## [0.5.2] - 2026-06-29

### Added

- Next.js showcase site and documentation.

## [0.5.1] - 2026-06-29

### Changed

- Tightened Patchright stealth defaults.

## [0.5.0] - 2026-06-26

Earlier releases (0.1.0 - 0.4.9) are recorded in the git history and tags only.

[Unreleased]: https://github.com/kacigaya/webskrap/compare/v3.1.0...HEAD
[3.1.0]: https://github.com/kacigaya/webskrap/compare/v3.0.1...v3.1.0
[3.0.1]: https://github.com/kacigaya/webskrap/compare/v3.0.0...v3.0.1
[3.0.0]: https://github.com/kacigaya/webskrap/compare/v2.15.0...v3.0.0
[2.15.0]: https://github.com/kacigaya/webskrap/compare/v2.14.0...v2.15.0
[2.14.0]: https://github.com/kacigaya/webskrap/compare/v2.13.3...v2.14.0
[2.13.3]: https://github.com/kacigaya/webskrap/compare/v2.13.2...v2.13.3
[2.13.2]: https://github.com/kacigaya/webskrap/compare/v2.13.1...v2.13.2
[2.13.1]: https://github.com/kacigaya/webskrap/compare/v2.13.0...v2.13.1
[2.13.0]: https://github.com/kacigaya/webskrap/compare/v2.12.0...v2.13.0
[2.12.0]: https://github.com/kacigaya/webskrap/compare/v2.11.0...v2.12.0
[2.11.0]: https://github.com/kacigaya/webskrap/compare/v2.10.1...v2.11.0
[2.10.1]: https://github.com/kacigaya/webskrap/compare/v2.10.0...v2.10.1
[2.10.0]: https://github.com/kacigaya/webskrap/compare/v2.9.0...v2.10.0
[2.9.0]: https://github.com/kacigaya/webskrap/compare/v2.8.0...v2.9.0
[2.8.0]: https://github.com/kacigaya/webskrap/compare/v2.7.0...v2.8.0
[2.7.0]: https://github.com/kacigaya/webskrap/compare/v2.6.0...v2.7.0
[2.6.0]: https://github.com/kacigaya/webskrap/compare/v2.5.0...v2.6.0
[2.5.0]: https://github.com/kacigaya/webskrap/compare/v2.4.0...v2.5.0
[2.4.0]: https://github.com/kacigaya/webskrap/compare/v2.3.0...v2.4.0
[2.3.0]: https://github.com/kacigaya/webskrap/compare/v2.2.0...v2.3.0
[1.0.2]: https://github.com/kacigaya/webskrap/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/kacigaya/webskrap/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/kacigaya/webskrap/compare/v0.10.1...v1.0.0
[0.10.1]: https://github.com/kacigaya/webskrap/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/kacigaya/webskrap/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/kacigaya/webskrap/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/kacigaya/webskrap/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/kacigaya/webskrap/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/kacigaya/webskrap/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/kacigaya/webskrap/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/kacigaya/webskrap/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/kacigaya/webskrap/compare/v0.5.9...v0.6.0
[0.5.9]: https://github.com/kacigaya/webskrap/compare/v0.5.8...v0.5.9
[0.5.8]: https://github.com/kacigaya/webskrap/compare/v0.5.7...v0.5.8
[0.5.7]: https://github.com/kacigaya/webskrap/compare/v0.5.6...v0.5.7
[0.5.6]: https://github.com/kacigaya/webskrap/compare/v0.5.5...v0.5.6
[0.5.5]: https://github.com/kacigaya/webskrap/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/kacigaya/webskrap/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/kacigaya/webskrap/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/kacigaya/webskrap/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/kacigaya/webskrap/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/kacigaya/webskrap/releases/tag/v0.5.0
