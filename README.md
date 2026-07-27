<p align="center">
  <img src="assets/webskrap-logo.png" alt="WebSkrap logo" width="200">
</p>

<h1 align="center">WebSkrap</h1>

<p align="center">
   <strong>Async-first Python scraping framework built on Playwright that also works as a web tool for LLMs and agents.</strong><br>
   <em>Coherent browser profiles, persistent sessions, resource routing, and Patchright-powered stealth for data collection that needs realistic browser behavior. Ships an MCP server so Claude, Codex, and any MCP agent can fetch live pages as plain text instead of raw HTML.</em>
</p>

Documentation: [https://kacigaya.github.io/webskrap/](https://kacigaya.github.io/webskrap/)

## Install

```bash
pip install webskrap
webskrap install
```

## Quickstart

```python
import asyncio

from webskrap import WebSkrapClient


async def main() -> None:
    async with WebSkrapClient() as client:
        result = await client.fetch("https://example.com")
        print(result.status)
        print(result.title)
        print(result.text[:200])


asyncio.run(main())
```

## Persistent session

```python
import asyncio
from pathlib import Path

from webskrap import SessionConfig, WebSkrapClient


async def main() -> None:
    config = SessionConfig(
        user_data_dir=Path(".webskrap/sessions/shop"),
        headless=True,
    )

    async with WebSkrapClient() as client:
        session = await client.session("shop", config=config, profile="desktop-chrome")
        first = await session.fetch("https://example.com")
        second = await session.fetch("https://example.com/account")
        print(first.final_url, second.final_url)


asyncio.run(main())
```

## Headed browser

Use a persistent session when you want the browser to stay open.

```python
import asyncio
from pathlib import Path

from webskrap import SessionConfig, WebSkrapClient


async def main() -> None:
    config = SessionConfig(
        headless=False,
        user_data_dir=Path(".webskrap/dev-session"),
    )

    async with WebSkrapClient() as client:
        session = await client.session("dev", config=config)
        page = await session.context.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded")

        input("Press Enter to close browser...")


asyncio.run(main())
```

## Human-like clicks

Use `human_click` when a manual interaction should include visible-element waits, scrolling,
short pauses, and mouse movement before the click.

```python
page = await session.context.new_page()
await page.goto("https://example.com", wait_until="domcontentloaded")
await session.human_click(page, "label[for='radio1']")
```

The cursor follows a cubic Bezier curve with randomized control points and
eased spacing, so it arcs toward the target and its speed ramps up then slows
near the end instead of tracing a straight, evenly spaced line. The curve
algorithm is adapted from [HumanCursor](https://github.com/riflosnake/HumanCursor);
WebSkrap reimplements it for Playwright's async mouse and adds no dependency.

Example for a headed Chrome session with a French desktop profile:

```python
import asyncio
from pathlib import Path

from webskrap import BrowserProfile, SessionConfig, Viewport, WebSkrapClient


async def main() -> None:
    config = SessionConfig(
        headless=False,
        channel="chrome",
        user_data_dir=Path(".webskrap/example"),
        navigation_timeout_ms=90_000,
        default_timeout_ms=90_000,
        slow_mo_ms=50,
        launch_args=[
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )
    profile = BrowserProfile(
        name="fr-desktop",
        viewport=Viewport(width=1440, height=900),
        screen=Viewport(width=1440, height=900),
        locale="fr-FR",
        timezone_id="Europe/Paris",
        navigator_languages=["fr-FR", "fr", "en-US", "en"],
    )

    async with WebSkrapClient() as client:
        session = await client.session("example", config=config, profile=profile)
        page = await session.context.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded")

        input("Press Enter to close browser...")


asyncio.run(main())
```

## Auto-decline cookie banners

Every `session.fetch()` (and therefore the CLI and the MCP tools) clicks the
reject control of a cookie consent notice before reading the page, so scraped
text is not buried under a banner and no optional cookies are accepted.

Detection is inspired by Brave's
[cookiecrumbler](https://github.com/brave/cookiecrumbler), which finds consent
notices; WebSkrap dismisses them. Two strategies, tried in every frame:

1. Reject buttons of the common consent platforms (OneTrust, Cookiebot, Didomi,
   Usercentrics, Sourcepoint, Quantcast, Osano, Complianz, CookieYes, ...).
2. A clickable whose label matches a reject phrase ("Reject all", "Refuser
   tout", "Ablehnen", "Continue without accepting", ...), scoped to a
   cookie/consent container so unrelated "Decline" buttons are never touched.

`FetchResult.cookie_notice_declined` reports which strategy clicked (`"cmp"`,
`"text"`, or `None`).

```python
config = SessionConfig(
    decline_cookies=True,             # default
    decline_cookies_timeout_ms=2_000,  # wait for a late-injected notice; 0 = check once
)

result = await client.fetch("https://example.com", config=config)
print(result.cookie_notice_declined)
```

The timeout is the per-fetch cost on pages without a notice. Pages that carry a
consent-platform iframe are retried for up to 3s while it renders. For pages you
drive yourself, call it directly:

```python
page = await session.context.new_page()
await page.goto("https://example.com", wait_until="domcontentloaded")
await session.decline_cookies(page)
```

Consent walls with no reject control on the first layer (pay-or-consent) are
left alone.

## Custom profile

```python
from webskrap import BrowserProfile, Viewport

profile = BrowserProfile(
    name="workstation",
    viewport=Viewport(width=1440, height=900),
    screen=Viewport(width=1440, height=900),
    locale="en-US",
    timezone_id="Europe/Paris",
)
```

## Session options

```python
from pathlib import Path

from webskrap import ProxyConfig, ResourcePolicy, SessionConfig

config = SessionConfig(
    browser="chromium",
    channel="chrome",
    headless=False,
    user_data_dir=Path(".webskrap/session"),
    storage_state=None,
    proxy=ProxyConfig(server="http://127.0.0.1:8080"),
    resource_policy=ResourcePolicy.LITE,
    decline_cookies=True,
    decline_cookies_timeout_ms=2_000,
    ignore_https_errors=True,
    java_script_enabled=True,
    service_workers="allow",
    timeout_ms=30_000,
    navigation_timeout_ms=90_000,
    default_timeout_ms=90_000,
    slow_mo_ms=50,
    launch_args=[
        "--start-maximized",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
    ],
)
```

`resource_policy` values:

- `ResourcePolicy.ALL`: allow all resources.
- `ResourcePolicy.LITE`: block images, fonts, and media.
- `ResourcePolicy.DOCUMENTS`: block images, fonts, media, and stylesheets.

## Profile options

```python
from webskrap import BrowserProfile, Viewport

profile = BrowserProfile(
    name="fr-desktop",
    user_agent=None,
    viewport=Viewport(width=1440, height=900),
    screen=Viewport(width=1440, height=900),
    locale="fr-FR",
    timezone_id="Europe/Paris",
    device_scale_factor=1,
    is_mobile=False,
    has_touch=False,
    color_scheme="light",
    reduced_motion="no-preference",
    extra_http_headers={},
    navigator_languages=["fr-FR", "fr", "en-US", "en"],
)
```

## Patchright stealth

The default `playwright` driver is detectable by CDP-aware bot detectors because
the DevTools Protocol `Runtime.enable` call leaks. For maximum stealth, switch to
the [`patchright`](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) driver, a
CDP-leak-free Playwright fork, and let the browser's real fingerprint show through.
WebSkrap does not inject JavaScript stealth patches.

`pip install webskrap` includes Patchright. Run `webskrap install` to download
browser binaries.

```python
from pathlib import Path

from webskrap import SessionConfig

config = SessionConfig(
    driver="patchright",
    channel="chrome",          # real Chrome beats anti-detect tampering checks
    headless=False,            # headed clears headless-only behavioral signals
    user_data_dir=Path(".webskrap/patchright-profile"),
)
```

With this configuration WebSkrap passes reCAPTCHA v3 (human score), renders
Cloudflare Turnstile without solving it, passes BrowserScan, the FingerprintJS
web-scraping demo, and deviceandbrowserinfo behavioral detection. See
[tests/test_bot_detection.py](tests/test_bot_detection.py) (run with
`WEBSKRAP_LIVE=1`). The `patchright` driver needs Google Chrome installed and the
`stealth` extra; without a `user_data_dir` it uses a throwaway persistent profile,
which patchright requires for full stealth.

Generate the live headed/headless graph report with:

```bash
$env:WEBSKRAP_LIVE=1
python scripts\live_stealth_report.py --no-open --report-only
```

Open `.webskrap/reports/live-stealth-results.html`.

For proxy DNS checks, set `WEBSKRAP_LIVE_EXPECTED_PUBLIC_IP` or
`WEBSKRAP_LIVE_EXPECTED_COUNTRY`.

## Comparison

CloakBrowser values below are copied from its
[upstream README](https://github.com/CloakHQ/CloakBrowser/blob/main/README.md).
WebSkrap values are from the local live report generated on 2026-06-26 with
`python scripts\live_stealth_report.py --no-open --report-only`.

| Feature | Playwright | playwright-stealth | undetected-chromedriver | Camoufox | CloakBrowser | WebSkrap patchright |
|---|---|---|---|---|---|---|
| reCAPTCHA v3 score | 0.1 | 0.3-0.5 | 0.3-0.7 | 0.7-0.9 | **0.9** | Pass in headed mode (`>=0.7` gate) |
| Cloudflare Turnstile | Fail | Sometimes | Sometimes | Pass | **Pass** | Renders challenge surface |
| Patch level | None | JS injection | Config patches | C++ (Firefox) | **C++ (Chromium)** | Patchright browser driver + native Chrome options |
| Survives Chrome updates | N/A | Breaks often | Breaks often | Yes | **Yes** | Depends on Chrome + Patchright compatibility |
| Maintained | Yes | Stale | Stale | Unstable | **Active** | Active project tests |
| Browser engine | Chromium | Chromium | Chrome | Firefox | **Chromium** | Chrome/Chromium |
| Playwright API | Native | Native | No (Selenium) | No | **Native** | Native-compatible |

| Detection Service | Stock Playwright | CloakBrowser | WebSkrap patchright headed | Notes |
|---|---|---|---|---|
| **reCAPTCHA v3** | 0.1 (bot) | **0.9** (human) | **PASS** | WebSkrap asserts score `>=0.7` when Google's demo returns one |
| **Cloudflare Turnstile** (non-interactive) | FAIL | **PASS** | **PASS** | Public demo renders the challenge surface; WebSkrap does not solve it |
| **FingerprintJS** bot detection | DETECTED | **PASS** | **PASS** | `demo.fingerprint.com/web-scraping` returns demo data |
| **BrowserScan** bot detection | DETECTED | **NORMAL** (4/4) | **PASS** | 0 abnormal checks in headed run |
| **bot.incolumitas.com** | 13 fails | **1 fail** | **PASS** | Only tolerated network/spec false positives |
| **deviceandbrowserinfo.com** | 6 true flags | **0 true flags** | **PASS** | `isBot: false` |
| **bot.sannysoft.com** | DETECTED | Not listed | **TIMEOUT** | Latest run timed out waiting for `networkidle` |
| **BrowserLeaks WebRTC** | Not listed | Not listed | **PASS** | No private ICE candidate IPs exposed |
| **BrowserLeaks Client Hints** | Not listed | Not listed | **PASS** | No `HeadlessChrome` token |
| **TLS / JA3 visibility** | Mismatch | **Identical to Chrome** | **PASS** | TLS/JA3/JA4 surface is visible; no proxy mismatch without proxy |
| **DNS leak standard test** | Not listed | Not listed | **PASS** | Resolver rows are public; optional proxy country/IP expectations supported |

Latest WebSkrap live summary: 24 passed, 2 failed, 1 skipped. Headed: 17
passed, 1 failed. Headless: 7 passed, 1 failed, 1 skipped. The two failures
were Sannysoft headed/headless `networkidle` timeouts; the headless skip was
reCAPTCHA v3 not returning a score from Google's public demo.

### Headless patchright

Headed patchright is the strongest stealth mode. For best-effort headless runs,
prefer real Chrome plus a stable persistent profile.

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

Headless mode is inherently more detectable than headed mode. WebSkrap keeps the
browser surfaces native instead of spoofing them with JavaScript, because broad
fingerprint patches can look like tampering.

Headless Chrome has no physical display, so it otherwise reports an 800x600
screen with zero `outerWidth`/`outerHeight`, an obvious headless tell. For
chromium headless runs WebSkrap configures a virtual screen at launch
(`--screen-info`, `--window-size`, `--window-position`), giving coherent
`screen`/`window` metrics without JavaScript spoofing. It defaults to 1920x1080;
override with `headless_screen=Viewport(width=..., height=...)` or disable with
`headless_screen=None`.

For fingerprint-statistics pages such as AmiUnique, opt in to Patchright context
profile metadata when you want profile locale/timezone/media settings applied
through native browser context options. This keeps `no_viewport=True` and avoids
JavaScript patches:

```python
config = SessionConfig(
    driver="patchright",
    channel="chrome",
    headless=True,
    mask_headless_user_agent=True,
    patchright_context_profile=True,
    reduce_fingerprint_surface=True,
    webrtc_ip_handling_policy="disable_non_proxied_udp",
)
```

The WebRTC policy prevents local or direct public ICE candidates from leaking to
WebRTC leak-test pages. It does not normalize unrelated high-entropy surfaces
such as fonts, canvas, battery, device memory, or TLS/session metadata.
`reduce_fingerprint_surface=True` reduces rendering entropy with native Chromium
flags, but pages that need WebGL or canvas export may not work correctly.

## CLI

`webskrap fetch` always runs headless Patchright stealth mode. `webskrap install`
downloads the browser binaries, and `webskrap doctor` verifies this CLI setup.

```bash
pip install webskrap
webskrap install
```

```bash
webskrap profiles
webskrap profiles --format json
webskrap doctor
webskrap doctor --format json
webskrap fetch https://example.com --profile desktop-chrome
webskrap fetch https://example.com --format json --max-chars 12000
webskrap fetch https://example.com --stdout --text-only
webskrap fetch https://example.com --screenshot example.png
webskrap fetch https://example.com --no-decline-cookies
webskrap fetch https://example.com --decline-cookies-timeout-ms 4000
webskrap fetch https://example.com --channel chrome \
  --user-data-dir .webskrap/headless-profile
webskrap fetch https://amiunique.org/fr/fingerprint \
  --channel chrome \
  --mask-headless-user-agent \
  --patchright-context-profile \
  --reduce-fingerprint-surface \
  --webrtc-ip-handling-policy disable_non_proxied_udp
```

Use repeated `--launch-arg=...` options for advanced browser flags.

The CLI checks PyPI once a day (best-effort) and prints an "update available"
notice to stderr when a newer `webskrap` is released. Set
`WEBSKRAP_NO_UPDATE_CHECK=1` to disable it; it is also skipped under `CI` and when
stderr is not a TTY.

## MCP server

WebSkrap ships a Model Context Protocol server so MCP clients (Claude Desktop,
Claude Code, Codex, ...) can drive a real browser directly. It exposes three
tools over stdio: `fetch`, `stealth_fetch`, and `doctor`.

Built for LLMs: both `fetch` and `stealth_fetch` run the same CDP-leak-free
Patchright stealth path the CLI uses (headless Chrome, `networkidle` wait), so
JS-heavy and anti-bot pages that block naive scrapers still load. They return
clean visible page text by default, with no HTML tags, scripts, or style noise,
so agents spend tokens on content, not markup (typically 5-10x fewer tokens than
raw HTML). Pass `text_only=False` when you actually need the HTML. Use
`stealth_fetch` for finer control over fingerprint surface, WebRTC, UA masking,
and persistent profiles. Both auto-decline cookie banners (`decline_cookies=False`
to keep them). Every result carries `status`, `final_url`, `title`,
`text_length`, `cookie_notice_declined`, and truncation flags so the model knows
exactly what it got.

```bash
pip install webskrap
webskrap install
webskrap-mcp
```

Register it with a client.

Claude Code:

```bash
claude mcp add webskrap -- webskrap-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "webskrap": {
      "command": "webskrap-mcp"
    }
  }
}
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.webskrap]
command = "webskrap-mcp"
args = []
```

MCP and Patchright are included by default; no extras are needed.

## Performance benchmarks

WebSkrap is a browser-automation framework, so these benchmarks measure what it
actually does: resource routing, session reuse, and concurrent fetching. They run
against a local HTTP server that serves a synthetic page referencing many delayed
sub-resources (images, stylesheets, media). No external sites are contacted, so
results are deterministic. Numbers below are from a single machine and will vary
with hardware; run them yourself with `python benchmarks.py`.

### Resource routing (full page load with delayed assets)

| Policy      | Time (ms) | vs ALL |
|-------------|:---------:|:------:|
| `DOCUMENTS` |  156.98   | 0.58x  |
| `LITE`      |  169.42   | 0.62x  |
| `ALL`       |  271.23   | 1.0x   |

Blocking images, fonts, and media (`LITE`) cuts load time ~38%; also dropping
stylesheets (`DOCUMENTS`) reaches ~42%.

### Session reuse

| Mode                  | Time (ms) | vs warm |
|-----------------------|:---------:|:-------:|
| Warm session reuse    |  215.74   |  1.0x   |
| Cold launch per fetch |  411.68   |  1.91x  |

Reusing a persistent session avoids per-fetch browser/context startup, roughly
2x faster than launching cold each time.

### Concurrency

Fetching 8 pages per batch from one session averages ~109 ms per page.

> Benchmarks average 20+ navigations after warm-up. See
> [benchmarks.py](benchmarks.py) for methodology.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```
