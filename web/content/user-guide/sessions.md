---
title: Persistent Browser Sessions for Web Scraping
description: Reuse cookies, local storage, and browser profiles across Python web scraping sessions.
---

# Sessions

A session keeps one browser context open across requests.

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

`client.session()` with the same name returns the open session:

```python
session = await client.session("default")
same_session = await client.session("default")
assert session is same_session
```

Close a session when you are done, or let `WebSkrapClient.close()` close them
all:

```python
await session.close()
await client.close_session("default")  # also forgets the named session
```

Wait for active fetches before you close. Cancelling one caller that waits for
a shared launch does not cancel the others. The client keeps ownership and
closes the browser on shutdown.

`session.fetch(..., decline_cookies=False)` overrides consent dismissal for one
call and leaves the shared config alone. Omit it to use
`SessionConfig.decline_cookies`. The four `solve_*` options work the same way
per call (see [Stealth](stealth.md#challenge-solvers)).

## Persistent storage

Set `user_data_dir` to keep cookies, local storage and profile state between
runs.

```python
config = SessionConfig(user_data_dir=Path(".webskrap/profile"))
```

When you do not need a full profile, use `storage_state`, a cookie and
local-storage snapshot in Playwright's format:

```python
config = SessionConfig(storage_state="state.json")
```

`storage_state` applies only to sessions without a persistent profile. With
`user_data_dir` set, the profile directory owns browser state.

Both hold real credentials: cookies, local storage and any logged-in session,
unencrypted, and they survive process exit. Keep them where only your user can
read them, and delete them when the work is done. `webskrap browser` does this
for its sessions: their directories under `~/.webskrap/browser/` are created
`0700` on POSIX.

## Headed debugging

To inspect behavior, set `headless=False` and keep a page open:

```python
import asyncio
from pathlib import Path

from webskrap import SessionConfig, WebSkrapClient


async def main() -> None:
    config = SessionConfig(
        headless=False,
        user_data_dir=Path(".webskrap/debug"),
    )

    async with WebSkrapClient() as client:
        session = await client.session("debug", config=config)
        page = await session.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded", timeout_s=30)
        input("Press Enter to close browser...")


asyncio.run(main())
```

## Human-like clicks

`human_click` clicks like a person instead of at the element's exact center.

```python
async with WebSkrapClient() as client:
    session = await client.session("default")
    page = await session.new_page()
    await page.goto("https://example.com", wait_until="domcontentloaded", timeout_s=30)
    await session.human_click(page, "label[for='radio1']")
```

Pass `human=False` to click the element's center at once, with the same checks.

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

`human_type` clicks into a field and types one key at a time with human
timing:

```python
await session.human_type(page, "input[name='q']", "running shoes")
```

The [Humanizer](/docs/user-guide/humanizer/) guide covers the behavior and its
safety limits.

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
