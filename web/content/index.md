---
title: WebSkrap Documentation
description: Documentation for WebSkrap, a Python web scraping and browser automation toolkit built on Playwright, Patchright, persistent sessions, resource routing, and MCP.
---

# WebSkrap

Async-first Python scraping on Playwright that also works as a web tool for LLMs and agents. You get coherent browser profiles, persistent sessions, resource routing, and Patchright-powered stealth. The MCP server runs that same stealth path, so agents get live pages back as plain text instead of raw HTML. Both the CLI and the MCP server can also drive persistent interactive browser sessions with Playwright CLI-style commands: aria snapshots with element refs, clicks, form fills, and screenshots against a browser that keeps running between calls.

## Install

```bash
pip install webskrap
webskrap install
```

## First fetch

```python
import asyncio

from webskrap import WebSkrapClient


async def main() -> None:
    async with WebSkrapClient() as client:
        result = await client.fetch("https://example.com")
        print(result.status)
        print(result.title)


asyncio.run(main())
```

## Explore

- [Installation](/docs/getting-started/installation)
- [Quickstart](/docs/getting-started/quickstart)
- [Client](/docs/user-guide/client)
- [Sessions](/docs/user-guide/sessions)
- [Profiles](/docs/user-guide/profiles)
- [Stealth](/docs/user-guide/stealth)
- [CLI](/docs/user-guide/cli)
- [MCP Server](/docs/user-guide/mcp)
- [API Reference](/docs/api-reference)
