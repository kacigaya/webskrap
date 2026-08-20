---
title: LLM Web Scraping and MCP Server for AI Agents
description: Use WebSkrap as an MCP web scraping server for LLM agents that need clean page text from live browser sessions instead of raw HTML.
---

# LLM Web Scraping and MCP Server for AI Agents

WebSkrap includes an MCP server so AI agents can fetch live web pages with a browser-backed scraper and receive clean page text instead of raw HTML.

Use it when an agent needs current page content without the scripts, styles, and markup noise that would otherwise fill the model's context.

## Start the MCP server

```bash
pip install webskrap
webskrap install
webskrap-mcp
```

The server exposes `fetch`, `stealth_fetch`, and `doctor`, plus `browser_*` tools for
persistent interactive sessions.

## CLI output for agents

The CLI also returns bounded JSON that is easy for agents to parse:

```bash
webskrap fetch https://example.com --format json --max-chars 12000
```

The JSON includes `url`, `final_url`, `status`, `ok`, `title`, `headers`, `text`, `text_length`, `text_truncated`, and `elapsed_ms`.

## Why clean text matters

Raw HTML spends thousands of tokens on tags, inline scripts, and styles the model never reads. Both fetch tools return visible page text by default, typically 5-10x smaller than the HTML they came from. Pass `text_only=false` when the markup itself is the point.

## Related docs

- [MCP Server](/docs/user-guide/mcp)
- [CLI](/docs/user-guide/cli)
- [Stealth](/docs/user-guide/stealth)
- [API Reference](/docs/api-reference)
