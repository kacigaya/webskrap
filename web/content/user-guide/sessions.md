---
title: Persistent Browser Sessions for Web Scraping
description: Reuse cookies, local storage, browser profiles, and Playwright contexts across Python web scraping sessions.
---

# Sessions

Sessions keep a browser context open across multiple requests.

```python
import asyncio
from pathlib import Path

from webskrap import SessionConfig, WebSkrapClient


async def main() -> None:
    config = SessionConfig(
        user_data_dir=Path(".webskrap/session"),
        headless=True,
    )

    async with WebSkrapClient() as client:
        session = await client.session("default", config=config)
        first = await session.fetch("https://example.com")
        second = await session.fetch("https://example.com/account")
        print(first.status, second.status)


asyncio.run(main())
```

Calling `client.session()` again with the same name returns the existing open
session:

```python
session = await client.session("default")
same_session = await client.session("default")
assert session is same_session
```

Close individual sessions when you are done with them, or let
`WebSkrapClient.close()` close all sessions:

```python
await session.close()
```

## Persistent storage

Set `user_data_dir` to keep cookies, local storage, and browser profile state between runs.

```python
config = SessionConfig(user_data_dir=Path(".webskrap/profile"))
```

Use `storage_state` for a Playwright-style cookie/local-storage snapshot when
you do not need a full persistent browser profile:

```python
config = SessionConfig(storage_state="state.json")
```

`storage_state` is applied only for non-persistent contexts. If
`user_data_dir` is set, the persistent profile directory owns browser state.

Both hold real credentials: cookies, local storage, and any logged-in session
you established, unencrypted and surviving process exit. Put them somewhere
only your user can read, and delete them when the work is done. The persistent
sessions managed by `webskrap browser` do this for you — their directories
under `~/.webskrap/browser/` are created `0700` on POSIX.

## Headed debugging

Set `headless=False` and keep a page open when you need to inspect behavior:

```python
import asyncio
from pathlib import Path

from webskrap import SessionConfig, WebSkrapClient


async def main() -> None:
    config = SessionConfig(
        headless=False,
        user_data_dir=Path(".webskrap/debug"),
        slow_mo_ms=50,
    )

    async with WebSkrapClient() as client:
        session = await client.session("debug", config=config)
        page = await session.context.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded")
        input("Press Enter to close browser...")


asyncio.run(main())
```

## Human-like clicks

Sessions expose `human_click` for interactions that should behave more like a manual click
than a direct Playwright `page.click`.

```python
async with WebSkrapClient() as client:
    session = await client.session("default")
    page = await session.context.new_page()
    await page.goto("https://example.com", wait_until="domcontentloaded")
    await session.human_click(page, "label[for='radio1']")
```

Pass `human=False` to use the same helper while delegating directly to Playwright.

```python
await session.human_click(page, "label[for='radio1']", human=False, timeout=5_000)
```

Pass normal click options such as `button`, `click_count`, `delay`, `modifiers`,
`position`, `strict`, and `trial`:

```python
await session.human_click(
    page,
    "button[type='submit']",
    strict=True,
    timeout=10_000,
    modifiers=["Shift"],
)
```

See the [Humanizer](/docs/user-guide/humanizer) guide for the focused behavior
reference and safety boundaries.

## Common options

```python
config = SessionConfig(
    browser="chromium",
    channel="chrome",
    headless=False,
    navigation_timeout_ms=90_000,
    default_timeout_ms=90_000,
    slow_mo_ms=50,
    launch_args=[
        "--start-maximized",
        "--no-first-run",
        "--no-default-browser-check",
    ],
)
```

## Proxy example

```python
from webskrap import ProxyConfig, SessionConfig

config = SessionConfig(
    proxy=ProxyConfig(
        server="http://127.0.0.1:8080",
        username="user",
        password="pass",
    ),
)
```

Do not commit proxy credentials, cookies, storage-state files, or persistent
session directories.
