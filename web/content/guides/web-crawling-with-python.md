---
title: Web Crawling with Python and WebSkrap
description: Build browser-aware Python web crawlers with WebSkrap, async concurrency, persistent sessions, resource policies, and safe crawling boundaries.
---

# Web Crawling with Python and WebSkrap

WebSkrap is useful for browser-aware web crawling when each URL may need JavaScript rendering, cookies, redirects, or a realistic browser profile.

A crawler should still be polite and bounded. Respect robots policies where applicable, rate limits, terms of service, and access controls. WebSkrap does not include CAPTCHA solving or access-control bypassing.

## Simple async crawl pattern

```python
import asyncio

from webskrap import WebSkrapClient

URLS = [
    "https://example.com",
    "https://example.com/about",
]


async def main() -> None:
    async with WebSkrapClient() as client:
        results = await asyncio.gather(*(client.fetch(url) for url in URLS))

    for result in results:
        print(result.status, result.final_url, result.title)


asyncio.run(main())
```

## Crawl with sessions

Use sessions when multiple pages belong to the same site and should share browser state.

```python
from pathlib import Path
from webskrap import SessionConfig, WebSkrapClient

config = SessionConfig(user_data_dir=Path(".webskrap/crawl-profile"))
```

Persistent sessions can keep cookies and local storage between fetches for sites you are authorized to access.

## Practical crawler tips

- Keep concurrency low until you understand a site.
- Use resource policies to avoid downloading heavy assets.
- Store `final_url` to detect redirects and canonical pages.
- Capture screenshots only for debugging because they add cost.
- Log status codes, titles, timings, and failures.

## Related docs

- [Client API](/docs/user-guide/client/)
- [Sessions](/docs/user-guide/sessions/)
- [Profiles](/docs/user-guide/profiles/)
- [Resource policy](/docs/user-guide/resource-policy/)
