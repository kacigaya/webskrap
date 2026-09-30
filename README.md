<p align="center">
  <img src="https://raw.githubusercontent.com/kacigaya/webskrap/main/assets/webskrap-logo.png" alt="WebSkrap logo" width="200">
</p>

<h1 align="center">WebSkrap</h1>

<p align="center">
  <strong>Async-first Python scraping on Playwright, with persistent sessions,
  resource routing, Patchright stealth, web search, a CLI, and an MCP server
  for agents.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/webskrap/"><img alt="WebSkrap version on PyPI" src="https://shieldcn.dev/pypi/webskrap.svg?variant=secondary&amp;logo=pypi"></a>
  <a href="https://www.python.org/"><img alt="Python 3.11 or newer" src="https://shieldcn.dev/badge/Python-3.11%2B-3776ab.svg?variant=secondary&amp;logo=python"></a>
  <a href="https://playwright.dev/python/"><img alt="Playwright 1.49 or newer" src="https://shieldcn.dev/badge/Playwright-1.49%2B-2ead33.svg?variant=secondary&amp;logo=playwright"></a>
  <a href="https://github.com/kacigaya/webskrap/blob/main/LICENSE"><img alt="Apache 2.0 License" src="https://shieldcn.dev/github/license/kacigaya/webskrap.svg?variant=secondary"></a>
</p>

## Install

```bash
pip install webskrap
webskrap install
```

## Quickstart

```python
import asyncio

from webskrap import WebSkrapClient


async def main() -> None:
    async with WebSkrapClient() as client:
        result = await client.fetch("https://example.com")
        print(result.status, result.title)
        print(result.text[:200])


asyncio.run(main())
```

## Search

A search loads the engine's results page in the same stealth browser and
returns the organic hits. Click-tracking redirects are unwrapped, so each
`url` is the destination itself. Bing is the default; use `engine="ddg"` for
DuckDuckGo's HTML endpoint. Google is not offered.

```python
async with WebSkrapClient() as client:
    result = await client.search("example domain", max_results=5)
    for hit in result.hits:
        print(hit.title, hit.url)
```

```bash
webskrap search "example domain" --engine ddg --max-results 10 --format json
```

When an engine distrusts the exit address it serves a bot challenge. That
comes back as a `blocked` error, not as zero hits. Switch engine, reuse a
persistent profile, or change the exit IP. WebSkrap does not retry and does
not solve CAPTCHAs.

## Visual browser interaction

Agents can see the rendered viewport and interact by coordinates, including
canvas and controls missing from the accessibility tree. MCP `browser_view`
returns a PNG image directly; `browser_mouse` clicks, moves, scrolls, or drags.
`browser_insert_text` inserts literal text into the focused control.

```bash
webskrap browser open https://example.com
webskrap browser view viewport.png --format json
# Open the returned PNG with your agent's image viewer, then choose coordinates.
webskrap browser mouse click 120 80
webskrap browser insert-text "hello"
webskrap browser press Enter
webskrap browser view viewport.png --format json
webskrap browser close
```

Coordinates are CSS pixels from the original image's top-left. Take a fresh
view after scrolling or page changes. Headless Chromium needs no Xvfb for
this; the agent's client and model must support image input.

## Documentation

- [Quickstart](https://kacigaya.github.io/webskrap/docs/getting-started/quickstart/)
- [Python API](https://kacigaya.github.io/webskrap/docs/api-reference/)
- [CLI](https://kacigaya.github.io/webskrap/docs/user-guide/cli/)
- [MCP server](https://kacigaya.github.io/webskrap/docs/user-guide/mcp/)
- [Stealth](https://kacigaya.github.io/webskrap/docs/user-guide/stealth/)
- [Benchmarks](https://kacigaya.github.io/webskrap/docs/benchmarks/)
- [Development](https://kacigaya.github.io/webskrap/docs/development/)

## Security

Persistent browser sessions keep Chromium's OS sandbox, store their profiles
`0700`, and confine MCP screenshot output to `./webskrap-output`. See
[SECURITY.md](SECURITY.md) for the security model and how to report a
vulnerability, and [CHANGELOG.md](CHANGELOG.md) for what changed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, and pull request
expectations.
