---
title: WebSkrap Client API for Browser Automation
description: Use WebSkrapClient for one-shot browser fetches, async scraping workflows, screenshots, and Playwright lifecycle management.
---

# Client

`WebSkrapClient` owns the Playwright lifecycle.

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

You can also manage the lifecycle manually when embedding WebSkrap in a larger
async application:

```python
client = WebSkrapClient()
await client.start()
try:
    result = await client.fetch("https://example.com")
finally:
    await client.close()
```

## One-shot fetch

`client.fetch()` creates a temporary browser session, opens the page, collects the response data, then closes that temporary session.

```python
result = await client.fetch(
    "https://example.com",
    wait_until="domcontentloaded",
    screenshot="example.png",
)
```

Use `client.session()` when you want to keep browser state or keep a headed browser open.

### Wait states

`wait_until` is passed to Playwright navigation and supports:

| Value | Use when |
| --- | --- |
| `commit` | You only need the navigation to begin. |
| `domcontentloaded` | You need parsed HTML and want a fast default. |
| `load` | You need regular page assets to finish loading. |
| `networkidle` | You need a quiet network, usually for JS-heavy pages. |

For pages with long-polling or analytics requests, `networkidle` can be slower
or less reliable than `domcontentloaded` plus an explicit page wait in a session.

### Screenshots and output

The Python API returns page HTML in `result.text` by default. Pass
`text_only=True` when you want readable body text instead:

```python
from pathlib import Path

result = await client.fetch("https://example.com", screenshot="example.png")
Path("example.html").write_text(result.text, encoding="utf-8")
print(result.screenshot_path)

text_result = await client.fetch("https://example.com", text_only=True)
print(text_result.text)
```

Pass `screenshot=True` to let WebSkrap choose a timestamped file name in the
current working directory.

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

Every `fetch()` clicks the reject control of a cookie consent notice before
reading the page, so banners do not bury the text and no optional cookies are
accepted. It knows the common consent platforms (OneTrust, Cookiebot, Didomi,
Usercentrics, Sourcepoint, ...) and falls back to matching reject wording inside
a cookie/consent container, in every frame.

```python
config = SessionConfig(
    decline_cookies=True,              # default
    decline_cookies_timeout_ms=2_000,  # wait for a late notice; 0 = check once
)
result = await client.fetch("https://example.com", config=config)
print(result.cookie_notice_declined)   # "cmp", "text", or None
```

For pages you drive yourself, call it on demand:

```python
page = await session.context.new_page()
await page.goto("https://example.com", wait_until="domcontentloaded")
await session.decline_cookies(page)
```

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

Check `status` before assuming `headers` or HTTP semantics exist:

```python
if result.status is None:
    print("Navigation did not return a main response")
elif result.ok:
    print("Loaded", result.final_url)
else:
    print("HTTP error", result.status)
```
