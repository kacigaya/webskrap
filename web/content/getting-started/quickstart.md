---
title: Python Web Scraping Quickstart
description: Start scraping web pages with WebSkrap, Playwright, async Python, screenshots, persistent browser state, and clean fetch results.
---

# Quickstart

## One-shot fetch

```python
import asyncio

from webskrap import WebSkrapClient


async def main() -> None:
    async with WebSkrapClient() as client:
        result = await client.fetch("https://example.com")
    print(result.status)
    print(result.final_url)
    print(result.title)
    print(result.text[:200])


asyncio.run(main())
```

`result.text` is the final page HTML. Use `result.ok`, `result.headers`, and
`result.cookies` when you need response metadata.

## Save output

Pass a screenshot path to capture the final page after navigation:

```python
result = await client.fetch(
    "https://example.com",
    wait_until="load",
    screenshot="example.png",
    timeout_ms=60_000,
)
print(result.screenshot_path)
```

Write HTML yourself when using the Python API:

```python
from pathlib import Path

Path("example.html").write_text(result.text, encoding="utf-8")
```

## Keep a headed browser open

Use a session when you want the browser to remain open for inspection.

```python
import asyncio
from pathlib import Path

from webskrap import SessionConfig, WebSkrapClient


async def main() -> None:
    config = SessionConfig(
        headless=False,
        user_data_dir=Path(".webskrap/dev-session"),
    )

    async with WebSkrapClient() as client:
        session = await client.session("dev", config=config)
        page = await session.context.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded")

        input("Press Enter to close browser...")


asyncio.run(main())
```

## Reuse browser state

Use `user_data_dir` when cookies, local storage, or profile state should survive
between runs:

```python
import asyncio
from pathlib import Path

from webskrap import SessionConfig, WebSkrapClient


async def main() -> None:
    config = SessionConfig(user_data_dir=Path(".webskrap/shop"), headless=True)

    async with WebSkrapClient() as client:
        session = await client.session("shop", config=config)
        first = await session.fetch("https://example.com")
        second = await session.fetch("https://example.com/account")
    print(first.status, second.final_url)


asyncio.run(main())
```

## Human-like clicks

Use `human_click` for manual interactions that should wait for the element, scroll it into
view with the mouse wheel, pause briefly, move the mouse, and then click with a human
button hold. `human_type` types into a field with human keystroke timing.

```python
page = await session.context.new_page()
await page.goto("https://example.com", wait_until="domcontentloaded")
await session.human_click(page, "label[for='radio1']")
await session.human_type(page, "input[name='q']", "running shoes")
```

## Profiles and resource policy

Pick a built-in profile by name, or pass a custom `BrowserProfile`.

```python
from webskrap import BrowserProfile, ResourcePolicy, SessionConfig, Viewport

profile = BrowserProfile(
    name="fr-desktop",
    viewport=Viewport(width=1440, height=900),
    screen=Viewport(width=1440, height=900),
    locale="fr-FR",
    timezone_id="Europe/Paris",
    navigator_languages=["fr-FR", "fr", "en-US", "en"],
)
config = SessionConfig(resource_policy=ResourcePolicy.LITE)

result = await client.fetch("https://example.com", profile=profile, config=config)
```

`ResourcePolicy.LITE` blocks images, fonts, and media. Use
`ResourcePolicy.DOCUMENTS` for HTML-focused extraction where stylesheets are not
needed.

## CLI fetch

```bash
webskrap fetch https://example.com
webskrap fetch https://example.com --format json --max-chars 12000
webskrap fetch https://example.com --stdout --text-only
webskrap fetch https://example.com --screenshot example.png
webskrap fetch https://example.com --output example.html --resource-policy lite
```
