---
title: Scrape JavaScript Pages with Python and Real Chromium
description: Use WebSkrap to scrape JavaScript-rendered pages in Python with real Chromium, wait conditions, screenshots, and resource routing.
---

# Scrape JavaScript Pages with Python and Real Chromium

Many pages do not put their main content in the initial HTML. WebSkrap drives
Chromium through its own DevTools Protocol client, so Python scripts can scrape
JavaScript-rendered pages after the browser loads them.

## Browser-backed fetch

```python
import asyncio

from webskrap import WebSkrapClient


async def main() -> None:
    async with WebSkrapClient() as client:
        result = await client.fetch(
            "https://example.com",
            wait_until="domcontentloaded",
            timeout_ms=30_000,
            screenshot="debug.png",
        )

    print(result.ok)
    print(result.title)
    print(result.screenshot_path)


asyncio.run(main())
```

## Faster scraping with resource routing

Images, fonts and media slow scraping down. A resource policy loads only what
the page needs.

```python
from webskrap import ResourcePolicy, SessionConfig

config = SessionConfig(resource_policy=ResourcePolicy.LITE)
```

Use `LITE` when pages need scripts or styles but not heavy media. `DOCUMENTS`
also drops stylesheets, for when unstyled HTML is enough.

## Debug JavaScript pages

For hard pages, keep a headed browser open:

```python
from pathlib import Path
from webskrap import SessionConfig

config = SessionConfig(
    headless=False,
    user_data_dir=Path(".webskrap/debug-profile"),
)
```

Check what the browser sees before you change the scraper.

## Related docs

- [Quickstart](/docs/getting-started/quickstart/)
- [Resource policy](/docs/user-guide/resource-policy/)
- [Sessions](/docs/user-guide/sessions/)
- [CLI](/docs/user-guide/cli/)
