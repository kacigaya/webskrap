---
title: WebSkrap Client API for Browser Automation
description: Use WebSkrapClient for one-shot browser fetches, async scraping workflows, screenshots, and browser lifecycle management.
---

# Client

`WebSkrapClient` owns the browser lifecycle. It starts a browser on first use
and closes every browser it started when it exits.

Use it as an async context manager:

```python
import asyncio

from webskrap import WebSkrapClient


async def main() -> None:
    async with WebSkrapClient() as client:
        result = await client.fetch("https://example.com")
    print(result.status)


asyncio.run(main())
```

To embed WebSkrap in a larger async application, manage the lifecycle yourself:

```python
client = WebSkrapClient()
await client.start()
try:
    result = await client.fetch("https://example.com")
finally:
    await client.close()
```

## One-shot fetch

`client.fetch()` opens a temporary browser session, loads the page, collects
the response, then closes the session.

```python
result = await client.fetch(
    "https://example.com",
    wait_until="domcontentloaded",
    screenshot="example.png",
)
```

To keep browser state or a headed browser open, use `client.session()`.

### Wait states

`wait_until` sets the load state that navigation waits for:

| Value | Use when |
| --- | --- |
| `commit` | You only need the navigation to begin. |
| `domcontentloaded` | You need parsed HTML and want a fast default. |
| `load` | You need regular page assets to finish loading. |
| `networkidle` | You need a quiet network, usually for JS-heavy pages. |

On pages with long-polling or analytics requests, `networkidle` can be slower
and less reliable than `domcontentloaded` plus an explicit wait in a session.

For deferred content, wait for a visible CSS selector or snapshot ref:

```python
result = await client.fetch(
    "https://example.com",
    ready_selector="#content",
    timeout_ms=30_000,
)
```

The wait starts after the cookie notice is dismissed. It gets its own
`timeout_ms` budget, or the configured navigation timeout. A missing selector
raises a timeout, and a blank selector is a usage error. `session.fetch()` takes
it too.

Known challenge pages raise `WebSkrapError` with code `blocked`, even with HTTP
200: DataDome, Cloudflare, HUMAN, Akamai, Fastly, AWS WAF, and reCAPTCHA or
hCaptcha on a 403, 429 or 503 page. So do Cloudflare and Akamai block pages.
`error.hint` says what to try for that kind of page. WebSkrap checks before
consent and extraction, and after a readiness timeout. Ordinary HTTP error
pages return `ok=False`. Detection uses known provider markers and headers, and
cannot find every challenge. Widgets are answered only with a `solve_*` option
(see [Stealth](stealth.md#challenge-solvers)).

### Screenshots and output

`result.text` holds the page HTML by default. Pass `text_only=True` for
readable body text:

```python
from pathlib import Path

result = await client.fetch("https://example.com", screenshot="example.png")
Path("example.html").write_text(result.text, encoding="utf-8")
print(result.screenshot_path)

text_result = await client.fetch("https://example.com", text_only=True)
print(text_result.text)
```

With `screenshot=True`, WebSkrap names the file
`webskrap-<32 hexadecimal characters>.png` in the current working directory.

### Per-call config

Pass a `SessionConfig` for one request without changing the client's defaults:

```python
from webskrap import ResourcePolicy, SessionConfig

config = SessionConfig(
    headless=True,
    resource_policy=ResourcePolicy.LITE,
    navigation_timeout_ms=60_000,
)
result = await client.fetch("https://example.com", config=config)
```

### Cookie banners

Cookie rejection is opt-in in Python and on by default in the CLI and MCP
server. When it is on, `fetch()` clicks the reject control before it reads the
page, so banners do not bury the text and no optional cookies are accepted.

```python
config = SessionConfig(
    decline_cookies=True,
    decline_cookies_timeout_ms=2_000,  # wait for a late notice; 0 = check once
)
result = await client.fetch("https://example.com", config=config)
print(result.cookie_notice_declined)  # "cmp", "text", or None
```

On pages you drive yourself, call it when you need it:

```python
page = await session.new_page()
await page.goto("https://example.com", wait_until="domcontentloaded", timeout_s=30)
await session.decline_cookies(page)
```

`decline_cookies_timeout_ms` is how long to wait for a notice on a page that
loads a consent platform. A page with no consent platform signal (no hidden
notice, TCF locator frame, or consent platform script or iframe) gets a 500 ms
check only, so most pages without a notice pay just that. Clicking adds at most
a 3 s internal budget, which covers retries while a consent platform's iframe
renders. After a `networkidle` wait, the notice wait drops to 500 ms, because
the consent script has had its chance to inject.

Detection draws on Brave's
[cookiecrumbler](https://github.com/brave/cookiecrumbler), which finds consent
notices. WebSkrap also dismisses them. Consent walls with no reject control on
the first layer (pay-or-consent) are left alone.

## FetchResult

`FetchResult` includes:

| Field | Meaning |
| --- | --- |
| `url` | URL requested by the caller. |
| `final_url` | Page URL after redirects and navigation. |
| `status` | Main response HTTP status, or `None` when no response is available. |
| `ok` | `True` when status is 2xx or 3xx. |
| `headers` | Main response headers. |
| `text` | Final page HTML from `page.content()`. |
| `title` | Final document title. |
| `cookies` | Browser-context cookies visible after the fetch. |
| `timings` | Timing data, currently including `elapsed_ms`. |
| `screenshot_path` | Screenshot path when screenshot capture was requested. |
| `cookie_notice_declined` | Strategy that dismissed a cookie consent notice (`cmp`, `text`), or `None`. |

Check `status` before you rely on `headers` or HTTP semantics:

```python
if result.status is None:
    print("Navigation did not return a main response")
elif result.ok:
    print("Loaded", result.final_url)
else:
    print("HTTP error", result.status)
```

### Select elements

`result.css(selector)` returns matching elements in document order.
`result.css_first(selector)` returns the first match or `None`. Each `Element`
has `tag`, `attrs`, `parent`, `children`, and `text`, and supports the same two
methods. Use `parse_html(html)` to query HTML from another source.

```python
from webskrap import WebSkrapClient

async with WebSkrapClient() as client:
    result = await client.fetch("https://example.com")
    for row in result.css("table.prices tr"):
        print([cell.text.strip() for cell in row.css("td")])
    link = result.css_first("a[href^='https://']")
    print(link.attrs["href"] if link else None)
```

Supported selectors are type and `*`, `#id`, `.class`, attribute tests
(`[a]`, `=`, `~=`, `|=`, `^=`, `$=`, `*=`), the descendant and `>`
combinators, and `,` lists. Other syntax, such as pseudo-classes and sibling
combinators, raises `ValueError`.

The parser runs offline on `result.text`. A `text_only` fetch returns plain
text, not HTML, so fetch without `text_only` before you query. The parser
handles void elements and common implied end tags. It does not run the full
HTML5 tree-construction algorithm, so badly broken markup can nest differently
than in the browser, and it does not insert implied elements such as `tbody`.
To query the browser's live tree, use a page from a session.
