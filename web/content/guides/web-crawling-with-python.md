---
title: Web Crawling with Python and WebSkrap
description: Build browser-aware Python web crawlers with WebSkrap, async concurrency, persistent sessions, resource policies, and safe crawling boundaries.
---

# Web Crawling with Python and WebSkrap

WebSkrap suits crawls where each URL may need JavaScript rendering, cookies,
redirects or a realistic browser profile.

Keep a crawler polite and bounded. Respect robots policies where they apply,
rate limits, terms of service and access controls. WebSkrap does not bypass
access controls. Its only CAPTCHA handling is four opt-in options: the
Cloudflare Turnstile click (`solve_cloudflare`), the DataDome slider drag
(`solve_datadome`), the HUMAN Press & Hold (`solve_perimeterx`), and the
reCAPTCHA or hCaptcha checkbox click (`solve_recaptcha`,
`browser_solve_captcha`). Use them responsibly.

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

Use a session when pages on one site should share browser state.

```python
from pathlib import Path
from webskrap import SessionConfig, WebSkrapClient

config = SessionConfig(user_data_dir=Path(".webskrap/crawl-profile"))
```

On sites you may access, persistent sessions keep cookies and local storage
between fetches.

## Practical crawler tips

- Keep concurrency low until you understand a site.
- Use resource policies to avoid downloading heavy assets.
- Store `final_url` to detect redirects and canonical pages.
- Capture screenshots only to debug, because they add cost.
- Log status codes, titles, timings, and failures.

## Related docs

- [Client API](/docs/user-guide/client/)
- [Sessions](/docs/user-guide/sessions/)
- [Profiles](/docs/user-guide/profiles/)
- [Resource policy](/docs/user-guide/resource-policy/)
