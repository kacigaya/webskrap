---
title: LLM Web Scraping and MCP Server for AI Agents
description: Use WebSkrap as an MCP web scraping server for LLM agents that need clean page text from live browser sessions instead of raw HTML.
---

# LLM Web Scraping and MCP Server for AI Agents

WebSkrap's MCP server lets AI agents fetch live pages in a real browser and get
back the visible text instead of raw HTML. The agent gets current content
without scripts, styles and markup filling the model's context.

## Start the MCP server

```bash
pip install webskrap
webskrap install
webskrap-mcp
```

The server exposes `fetch`, `stealth_fetch`, `search` and `doctor`, plus
`browser_*` tools for persistent interactive sessions.

## CLI output for agents

The CLI returns size-capped JSON that agents can parse:

```bash
webskrap fetch https://example.com --format json --max-chars 12000
```

The JSON includes `url`, `final_url`, `status`, `ok`, `title`, `headers`,
`text`, `text_length`, `text_truncated` and `elapsed_ms`.

## Why clean text matters

Raw HTML spends tokens on tags, inline scripts and styles the model never
reads. Both fetch tools return visible page text by default. Pass
`text_only=false` when you need the markup.

## Related docs

- [MCP Server](/docs/user-guide/mcp/)
- [CLI](/docs/user-guide/cli/)
- [Stealth](/docs/user-guide/stealth/)
- [API Reference](/docs/api-reference/)
