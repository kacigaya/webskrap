---
title: Install WebSkrap for Python Web Scraping
description: Install WebSkrap, its Chrome for Testing browser, and the CLI for Python web scraping and browser automation.
---

# Installation

Install WebSkrap from PyPI:

```bash
pip install webskrap
```

Download Chrome for Testing, the Chromium build that Google publishes for
automation. It goes in your user cache folder; set `WEBSKRAP_BROWSERS_PATH` to
use another folder:

```bash
webskrap install
```

Verify the installation:

```bash
webskrap doctor
webskrap profiles
```

## Included by default

`pip install webskrap` includes the Python dependencies for:

- WebSkrap's own Chrome DevTools Protocol client. It needs no Playwright,
  Patchright or Node.js.
- The `webskrap-mcp` server.

No extras are needed; everything above ships with the base install.

Use machine-readable setup output in automation:

```bash
webskrap install --format json
webskrap doctor --format json
```

## Development install

For local development:

```bash
git clone https://github.com/kacigaya/webskrap.git
cd webskrap
pip install -e ".[dev]"
webskrap install
```

Run checks:

```bash
pytest -q
ruff check .
(cd web && bun run build)
```

If you use `uv`, the same checks can run through the project environment:

```bash
uv run pytest -q
uv run ruff check .
```
