---
title: Scrape JavaScript Pages with Python and Real Chromium
description: Use WebSkrap to scrape JavaScript-rendered pages in Python with real Chromium, wait conditions, screenshots, and resource routing.
---

# Scrape JavaScript Pages with Python and Real Chromium

Many web pages do not expose their main content in the initial HTML. WebSkrap drives Chromium through its own DevTools Protocol client so Python scripts can scrape JavaScript-rendered pages after the browser has loaded them.

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

Images, fonts, media, and trackers often slow scraping down. WebSkrap supports resource policies so you can load only what the page needs.

```python
from webskrap import ResourcePolicy, SessionConfig

config = SessionConfig(resource_policy=ResourcePolicy.LITE)
```

Use `DOCUMENTS` when you only need top-level document HTML. Use `LITE` when pages need scripts or styles but not heavy media.

## Debug JavaScript pages

For tricky pages, keep a headed browser open:

```python
from pathlib import Path
from webskrap import SessionConfig

config = SessionConfig(
    headless=False,
    user_data_dir=Path(".webskrap/debug-profile"),
)
```

Then inspect what the browser actually sees before changing the scraper.

## Related docs

- [Quickstart](/docs/getting-started/quickstart/)
- [Resource policy](/docs/user-guide/resource-policy/)
- [Sessions](/docs/user-guide/sessions/)
- [CLI](/docs/user-guide/cli/)
