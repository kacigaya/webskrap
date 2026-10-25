---
title: Python Web Scraping with WebSkrap
description: Learn Python web scraping with WebSkrap, real Chromium, browser profiles, persistent sessions, and clean text extraction for modern JavaScript pages.
---

# Python Web Scraping with WebSkrap

WebSkrap is a Python web scraping toolkit for pages that need a real browser. It drives Chromium through its own Chrome DevTools Protocol client, with an async API, reusable browser sessions, coherent browser profiles, resource routing, and stealth defaults.

Use it when a simple `requests` or `BeautifulSoup` script cannot see the content because the site renders data with JavaScript, relies on browser storage, or needs a realistic browser context.

## Install

```bash
pip install webskrap
webskrap install
```

## Scrape a page

```python
import asyncio

from webskrap import WebSkrapClient


async def main() -> None:
    async with WebSkrapClient() as client:
        result = await client.fetch("https://example.com")

    print(result.status)
    print(result.final_url)
    print(result.title)
    print(result.text[:500])


asyncio.run(main())
```

`FetchResult` includes the final URL, status, headers, cookies, title, HTML/text output, timings, and optional screenshot path.

## Why use a browser scraper?

Modern websites often ship a small HTML shell and fill it with JavaScript. WebSkrap launches Chromium and drives it over CDP, so your scraper sees the page after browser navigation, redirects, cookies, and client-side rendering.

Common use cases:

- Scrape JavaScript-heavy pages in Python.
- Build data collection scripts that need browser state.
- Reuse login or consent state with persistent sessions on sites you are allowed to access.
- Return clean page data to LLM agents through MCP.
- Capture screenshots for debugging.

## Next steps

- [Quickstart](/docs/getting-started/quickstart/)
- [Client API](/docs/user-guide/client/)
- [Persistent sessions](/docs/user-guide/sessions/)
- [Browser profiles](/docs/user-guide/profiles/)
- [Resource policy](/docs/user-guide/resource-policy/)
