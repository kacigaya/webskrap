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

The Python API defaults to Playwright. Opt into Patchright through
`SessionConfig`:

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

## Consent and links

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

## Detection-sensitive pages

Headed Patchright is the strongest available mode for strict detection
surfaces:

```python
config = SessionConfig(driver="patchright", channel="chrome", headless=False)
```

For fingerprint-statistics or WebRTC leak-test pages, prefer native browser
controls over JavaScript patches:

```python
config = SessionConfig(
    driver="patchright",
    channel="chrome",
    headless=True,
    patchright_context_profile=True,
    reduce_fingerprint_surface=True,
    mask_headless_user_agent=True,
    webrtc_ip_handling_policy="disable_non_proxied_udp",
)
```
