<p align="center">
  <img src="assets/webskrap-logo.png" alt="WebSkrap logo" width="200">
</p>

<h1 align="center">WebSkrap</h1>

<p align="center">
  <strong>Async-first Python scraping on Playwright, with persistent sessions,
  resource routing, Patchright stealth, web search, a CLI, and an MCP server
  for agents.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/webskrap/"><img alt="WebSkrap 1.0.0 on PyPI" src="https://shieldcn.dev/pypi/webskrap.svg?variant=secondary&amp;logo=pypi"></a>
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

Searches load the engine's own results page in the same stealth browser and
return the organic hits with click-tracking unwrapped. DuckDuckGo's HTML
endpoint is the default; Bing is the alternative. Google is not offered.

```python
async with WebSkrapClient() as client:
    result = await client.search("example domain", max_results=5)
    for hit in result.hits:
        print(hit.title, hit.url)
```

```bash
webskrap search "example domain" --engine ddg --max-results 10 --format json
```

An engine that distrusts the exit address serves a bot challenge; that is
reported as a `blocked` error, not as zero hits. Switch engine, reuse a
persistent profile, or change exit IP. Nothing is retried and no CAPTCHA is
solved.

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
