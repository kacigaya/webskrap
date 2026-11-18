<p align="center">
  <img src="https://raw.githubusercontent.com/kacigaya/webskrap/main/assets/webskrap-logo.svg" alt="WebSkrap logo" width="200">
</p>

<h1 align="center">WebSkrap</h1>

<p align="center">
  <strong>Async-first Python scraping on its own Chrome DevTools Protocol
  client, with persistent sessions, resource routing, stealth defaults, web
  search, a CLI, and an MCP server for agents.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/webskrap/"><img alt="WebSkrap version on PyPI" src="https://shieldcn.dev/pypi/webskrap.svg?variant=secondary&amp;logo=pypi"></a>
  <a href="https://www.python.org/"><img alt="Python 3.11 or newer" src="https://shieldcn.dev/badge/Python-3.11%2B-3776ab.svg?variant=secondary&amp;logo=python"></a>
  <a href="https://github.com/kacigaya/webskrap/blob/main/LICENSE"><img alt="Apache 2.0 License" src="https://shieldcn.dev/github/license/kacigaya/webskrap.svg?variant=secondary"></a>
</p>

<p align="center">
  <a href="https://github.com/kacigaya/webskrap/blob/main/assets/demo.mp4"><img src="https://raw.githubusercontent.com/kacigaya/webskrap/main/assets/demo-poster.jpg" alt="Play the 24-second WebSkrap demo video: real Chrome passes public bot tests, and an AI agent drives it through the WebSkrap MCP server." width="760"></a>
</p>

## Install

```bash
pip install webskrap
webskrap install
```

`webskrap install` downloads Chrome for Testing. WebSkrap drives it through its
own CDP client, with no Playwright. Upgrading from 2.x? Read the
[migration guide](docs/migration-3.0.md).

## Quickstart

```python
import asyncio

from webskrap import WebSkrapClient


async def main() -> None:
    async with WebSkrapClient() as client:
        result = await client.fetch("https://example.com")
        print(result.status, result.title)
        print(result.text[:200])


asyncio.run(main())
```

For deferred content, pass `ready_selector="#content"` to a Python fetch or
`--ready-selector '#content'` to CLI `fetch`. Both MCP fetch tools accept
`ready_selector` too. The selector must become visible after consent dismissal.
Known challenge pages (DataDome, Cloudflare, HUMAN, Akamai, AWS WAF, and reCAPTCHA or hCaptcha on a 403, 429 or 503 page) and provider block
pages (DataDome, Cloudflare, Akamai) raise `blocked`. WebSkrap waits for device
checks that reload the page. It does not retry refusals. With
`solve_cloudflare=True` (CLI `--solve-cloudflare`), it clicks Cloudflare's
Turnstile checkbox; see [Cloudflare Turnstile](#cloudflare-turnstile). With
`solve_datadome=True` (CLI `--solve-datadome`), it drags DataDome's slider; see
[DataDome slider](#datadome-slider). With `solve_perimeterx=True` (CLI
`--solve-perimeterx`), it holds HUMAN's Press & Hold button; see
[HUMAN Press & Hold](#human-press--hold). With `solve_recaptcha=True` (CLI
`--solve-recaptcha`), it clicks a reCAPTCHA v2 or hCaptcha checkbox on a
blocking page; see [reCAPTCHA and hCaptcha checkbox](#recaptcha-and-hcaptcha-checkbox).
Ordinary HTTP errors still return `ok=False`.
For an empty HTTP error body, Chromium can show its own error page. The returned
text or HTML is that rendered page. The status, headers, and `final_url` still
come from the HTTP response.

For any other CAPTCHA, complete it by hand: open a persistent browser with
`webskrap browser open --headed --session manual --proxy http://localhost:8888 URL`.
Complete the check in that browser. Keep the same session, profile, and proxy
for later commands. On a remote host, use a desktop connection to its display.
`virtual_display=True` alone does not provide a remote desktop.

Keep `profile_mode="native"` for the browser's own identity. On Linux with Xvfb,
`virtual_display=True` runs a headed browser without a visible desktop window.
Full profile emulation is useful for device tests, but cannot replace every
worker or iframe value. See the [identity limits](src/webskrap/guide.md).

## Python batches

Use `client.fetch_many(urls)` to fetch a finite batch in order with one browser:

```python
async with WebSkrapClient() as client:
    results = await client.fetch_many(
        ["https://example.com", "https://example.org"], text_only=True
    )
```

Each batch shares cookies and storage, then closes its browser. Separate batches
and `client.fetch()` calls use fresh sessions. An explicit `user_data_dir` can
still share state on disk. Empty batches do not launch a browser. The first page
error stops the batch and raises; no partial list is returned. Ordinary HTTP
errors remain results with `ok=False`. `screenshot=True` uses a separate generated
PNG filename for each page.

For large inputs, per-page options, or state across batches, create a session
with `session = await client.session("crawl")`. Call `await session.fetch(url)`
for each URL. Close it with `await client.close_session("crawl")`, or let the
client context close it. Session names belong to one client. Use separate
sessions for unrelated callers.

## Select elements

Query fetched HTML with CSS selectors. Use `parse_html(html)` for HTML from
other sources.

```python
async with WebSkrapClient() as client:
    result = await client.fetch("https://example.com")
    print(result.css_first("h1").text)
    print([a.attrs["href"] for a in result.css("a[href]")])
```

## Search

A search loads the engine's results page in the same stealth browser and
returns the organic hits. Click-tracking redirects are unwrapped, so each
`url` is the destination itself. Bing is the default; use `engine="ddg"` for
DuckDuckGo's HTML endpoint. Google is not offered.

```python
async with WebSkrapClient() as client:
    result = await client.search("example domain", max_results=5)
    for hit in result.hits:
        print(hit.title, hit.url)
```

```bash
webskrap search "example domain" --engine ddg --max-results 10 --format json
```

When an engine distrusts the exit address it serves a bot challenge. That
comes back as a `blocked` error, not as zero hits. Switch engine, reuse a
persistent profile, or change the exit IP. WebSkrap does not retry.

## Cloudflare Turnstile

`solve_cloudflare=True` (CLI `--solve-cloudflare`) handles Cloudflare's "Just
a moment" page, like Scrapling's option of the same name. It is off by default;
without it a Cloudflare challenge fails fast with `blocked`. With it, WebSkrap
waits on the challenge page. When Cloudflare draws its Turnstile checkbox, the
cursor wanders for 1.5 to 3 seconds, follows a curved path to the checkbox and
clicks with a mouse-like button hold. The checkbox position is read from the
widget's own DOM before each click. If the page has not cleared 20 seconds
later, WebSkrap clicks again, at most three times. The checkbox gets up to 60
seconds from when it first appears, on top of `timeout_ms`. Success is reported
only after the real page loads and no challenge is detected on it; otherwise
the call fails with `blocked`.

It works in headless, headed and `virtual_display` modes. A click only answers
the widget: Cloudflare still scores the browser and the exit IP, so a headless
browser or a flagged IP can still be refused. No token is read, copied or
injected, and no outside solving service is used. Other CAPTCHAs, such as
image and audio puzzles, are reported as `blocked` and never touched.

## DataDome slider

`solve_datadome=True` (CLI `--solve-datadome`) handles DataDome's slider
CAPTCHA. It is off by default; without it a DataDome CAPTCHA fails fast with
`blocked`. With it, WebSkrap waits on the CAPTCHA page. When DataDome draws
its slider, the cursor wanders for 3 to 5 seconds, moves to the handle, grabs
it and drags it onto the slot in 35 small moves: slow, then faster, then slow
again, with a little jitter. The handle and slot positions are read from the
CAPTCHA frame's DOM before each drag. If the page has not cleared 10 seconds
later, WebSkrap drags again, at most three times. The slider gets up to 60
seconds from when it first appears, on top of `timeout_ms`. Success is reported
only after the real page loads and no challenge is detected on it; otherwise
the call fails with `blocked`.

A drag only answers the slider: DataDome still scores the browser, the exit IP
and the gesture. A hard block has no slider and stays `blocked`. Headed and
`virtual_display` browsers passed in live runs; a headless browser can get the
hard block instead. No cookie or token is read, copied or injected, no outside
solving service is used, and DataDome's audio and image puzzles are never
touched.

## HUMAN Press & Hold

`solve_perimeterx=True` (CLI `--solve-perimeterx`) handles the Press & Hold
challenge of HUMAN Security (formerly PerimeterX). It is off by default;
without it the challenge page fails fast with `blocked`. With it, WebSkrap
waits on the challenge page. When HUMAN draws its button, the cursor follows a
curved path to it, presses, and holds with a small hand tremor until the
button's progress fill is complete, then lets go. The button is read from the
challenge frame's DOM before each hold. If the page has not cleared 8 seconds
later, WebSkrap holds again, at most three times. The button gets up to 60
seconds from when it first appears, on top of `timeout_ms`. Success is
reported only after the real page loads and no challenge is detected on it;
otherwise the call fails with `blocked`.

A hold only answers the widget: HUMAN still scores the browser and the exit
IP. In live runs it refused every completed hold from a flagged IP, in every
browser mode, and accepted the first hold from a cleaner exit. No cookie or
token is read, copied or injected, and no outside solving service is used.

## reCAPTCHA and hCaptcha checkbox

`solve_recaptcha=True` (CLI `--solve-recaptcha`) clicks the "I'm not a robot"
checkbox of reCAPTCHA v2 or hCaptcha on a page that blocks the site: a 403,
429 or 503 response that carries the widget. It is off by default; without it
such a page fails fast with `blocked`. With it, WebSkrap waits for the widget,
moves the cursor like a person and clicks the checkbox. When the provider
trusts the browser, the box ticks and the page's own callback continues to the
real page. If the page has not cleared 15 seconds later, WebSkrap clicks again,
at most twice within 60 seconds. When the provider opens an image challenge
instead, WebSkrap leaves it alone and the call fails with `blocked` at once.

For a checkbox inside a form, fill the form, then call `browser_solve_captcha`
(CLI `webskrap browser solve-captcha`) before you submit it. Its
`captcha.state` is `checked`, `challenge`, `gone`, `timeout` or `not_found`.

The click only answers the checkbox: the provider still scores the browser and
the exit IP, and a datacenter IP usually gets the image challenge. reCAPTCHA
v3 and invisible widgets have no checkbox and are not handled. No token is
read, copied or injected, and no outside solving service is used.

Use them responsibly: only on sites whose terms and applicable law allow
automated access, and at a polite rate. Passing a challenge does not grant
permission to collect data you may not collect.

```bash
webskrap fetch https://example.com --virtual-display --solve-cloudflare
webskrap fetch https://example.com --virtual-display --solve-datadome
webskrap fetch https://example.com --virtual-display --solve-perimeterx
webskrap fetch https://example.com --virtual-display --solve-recaptcha
webskrap browser solve-captcha
```

## Visual browser interaction

Agents can see the rendered viewport and interact by coordinates, including
canvas and controls missing from the accessibility tree. MCP `browser_view`
returns a PNG image directly; `browser_mouse` clicks, moves, scrolls, or drags.
`browser_insert_text` inserts literal text into the focused control.

```bash
webskrap browser open https://example.com
webskrap browser view viewport.png --format json
# Open the returned PNG with your agent's image viewer, then choose coordinates.
webskrap browser mouse click 120 80
webskrap browser insert-text "hello"
webskrap browser press Enter
webskrap browser view viewport.png --format json
webskrap browser close
```

Coordinates are CSS pixels from the original image's top-left. Take a fresh
view after scrolling or page changes. Headless Chromium needs no Xvfb for
this; the agent's client and model must support image input.

## Load only what the task needs
The default resource policy is `auto`. Text-only fetches skip images, fonts
and media. HTML fetches, screenshots and interactive pages load all resources.
Set `resource_policy="all"`, `"lite"`, or `"documents"` to override this choice.
Use `documents` only when removing CSS preserves the required result.
Fetches use `domcontentloaded`; set `ready_selector` for deferred content.
Use `browser_find(text="Save")` to locate a known control with ancestor context.
Use `browser_snapshot(target="e15", depth=2)` for one subtree; add `boxes=true`
only when geometry matters. Reuse returned refs with `browser_interact`.
Use `networkidle`, screenshots and full snapshots only when the task needs them.
CLI and MCP reject cookies by default. Disable this only for known pages where
it is unnecessary. In Python, enable rejection with `SessionConfig(decline_cookies=True)`.

## Documentation

- [Agent skill](.agents/skills/webskrap/SKILL.md)
- [Quickstart](https://kacigaya.github.io/webskrap/docs/getting-started/quickstart/)
- [Python API](https://kacigaya.github.io/webskrap/docs/api-reference/)
- [CLI](https://kacigaya.github.io/webskrap/docs/user-guide/cli/)
- [MCP server](https://kacigaya.github.io/webskrap/docs/user-guide/mcp/)
- [Stealth](https://kacigaya.github.io/webskrap/docs/user-guide/stealth/)
- [Benchmarks](https://kacigaya.github.io/webskrap/docs/benchmarks/)
- [Development](https://kacigaya.github.io/webskrap/docs/development/)

The agent skill is available at `.agents/skills/webskrap/SKILL.md`,
`.claude/skills/webskrap/SKILL.md`, and `.codex/skills/webskrap/SKILL.md`.
The file lives in `.agents/skills/webskrap/`. The `.claude` and `.codex`
folders link to it, and every folder links to the root `references/`.
The checkout must support symbolic links. On Windows, enable Developer Mode
and Git's `core.symlinks` before cloning.

## Security

Persistent browser sessions keep Chromium's OS sandbox, store their profiles
`0700`, and confine MCP screenshot output to `./webskrap-output`. See
[SECURITY.md](SECURITY.md) for the security model and how to report a
vulnerability, and [CHANGELOG.md](CHANGELOG.md) for what changed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, and pull request
expectations.

<!-- mcp-name: io.github.kacigaya/webskrap -->
