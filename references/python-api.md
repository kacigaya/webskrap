# Python API

Read this reference for Python API work. Confirm signatures and defaults in
`src/webskrap/client.py` and `src/webskrap/models.py` before changing code.

## Fetch or session

Use `WebSkrapClient` as an async context manager. `client.fetch()` handles a
one-shot request. Use `client.session()` when cookies, storage, manual page
work, headed debugging, or interactions must persist.

```python
import asyncio

from webskrap import WebSkrapClient


async def main() -> None:
    async with WebSkrapClient() as client:
        result = await client.fetch("https://example.com", include_links=True)
    print(result.status, result.final_url, result.title)
    print(result.text[:200])


asyncio.run(main())
```

WebSkrap 3 drives Chromium through its own CDP client. There is no driver to
choose. Pick the browser with `channel` (`chrome`, `msedge`, `chromium`):

```python
from pathlib import Path

from webskrap import SessionConfig

config = SessionConfig(
    channel="chrome",
    headless=True,
    user_data_dir=Path(".webskrap/headless-profile"),
)
```

`channel=None` or `"chromium"` uses the Chrome for Testing build from
`webskrap install`.

## Pages you drive yourself

`session.new_page()` returns a `webskrap.cdp.CDPPage` with the session's
profile and policies applied. Close it with `session.close_page(page)`.

```python
async with WebSkrapClient() as client:
    session = await client.session("manual")
    page = await session.new_page()
    response = await page.goto("https://example.com", wait_until="load", timeout_s=30)
    title = await page.evaluate("document.title")
    await session.human_click(page, "a")
    await session.close_page(page)
```

- `page.goto()` returns the CDP `Response` object, for example `status` and
  `headers`.
- `page.evaluate()` runs in an isolated world. It shares the DOM with the page
  but not the page's JavaScript globals.
- `session.human_click()` and `session.human_type()` take a CSS selector or a
  snapshot ref.
- `page.send(method, params)` sends any other CDP command to the page.

## Consent and links

`client.fetch()` and `session.fetch()` accept `ready_selector`, an optional
CSS selector or snapshot ref that must become visible after consent dismissal and before
extraction. Its wait uses `timeout_ms` or `navigation_timeout_ms` separately from
navigation. A missing selector raises a timeout. A blank selector is a usage error.

Known DataDome and Cloudflare challenge pages raise `WebSkrapError` with
`code=ErrorCode.BLOCKED`, even when served with HTTP 200. Ordinary HTTP errors
still return `FetchResult(ok=False)`. Detection uses active provider markers and
headers; it does not identify every challenge or solve verification widgets.

Cookie rejection is opt-in for Python callers. The CLI and MCP server enable it
by default.

```python
config = SessionConfig(decline_cookies=True, decline_cookies_timeout_ms=2_000)
```

`FetchResult.cookie_notice_declined` reports `"cmp"`, `"text"`, or `None`.
`session.decline_cookies(page)` applies the same handling to a page controlled
by the caller.

Link collection is opt-in with `include_links=True` and capped by `max_links`.
Links resolve against the final URL. Duplicate and `javascript:` targets are
dropped. `links_total` is the count before the cap. A session with
`java_script_enabled=False` cannot collect links.

## Search

`client.search(query, engine=..., max_results=...)` and
`session.search(...)` load the engine's results page through the same fetch
path, so `SessionConfig` proxy, consent, and profile settings apply. Engines
are `SearchEngine.BING` (default) and `SearchEngine.DDG`; Google is not
offered.

```python
from webskrap import SearchEngine, WebSkrapClient

async with WebSkrapClient() as client:
    result = await client.search("example domain", engine=SearchEngine.BING, max_results=5)
for hit in result.hits:
    print(hit.title, hit.url, hit.snippet)
```

`SearchResult.hits` is capped by `max_results`; `hits_total` counts the page.
Destinations are unwrapped from the engine's click redirects and deduplicated.
A bot challenge raises `WebSkrapError` with code `blocked`; nothing is retried.

## Detection-sensitive pages

Headed Chrome is the strongest available mode for strict detection surfaces:

```python
config = SessionConfig(channel="chrome", headless=False)
```

For fingerprint-statistics or WebRTC leak-test pages, prefer native browser
controls over JavaScript patches:

```python
config = SessionConfig(
    channel="chrome",
    headless=True,
    profile_mode="locale",
    reduce_fingerprint_surface=True,
    virtual_display=True,
    webrtc_ip_handling_policy="disable_non_proxied_udp",
)
```

`profile_mode` sets how much of the `BrowserProfile` pages see:

- `"native"` (default): nothing. The browser shows its own identity.
- `"locale"`: locale, timezone, color scheme, reduced motion and extra headers.
  On Linux, locale and timezone come from the browser environment.
- `"full"`: also viewport, screen, device scale, touch and user agent, as
  WebSkrap 2.x did. Each override is a surface that a page can cross-check.
