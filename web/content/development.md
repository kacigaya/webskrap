---
title: Developing WebSkrap
description: Development notes for contributing to WebSkrap, running tests, browser integrations, and maintaining the Python scraping package.
---

# Development

Clone the repository:

```bash
git clone https://github.com/kacigaya/webskrap.git
cd webskrap
```

Install development dependencies:

```bash
pip install -e ".[dev]"
webskrap install
```

Run tests and lint:

```bash
pytest -q
ruff check .
```

Build the web docs:

```bash
cd web
bun install
bun run build
```

Use the opt-in live tests only when you need to verify third-party bot-detection
behavior:

```bash
WEBSKRAP_LIVE=1 pytest -q -m live
```

They require network access, Patchright, an installed browser, and public demo
sites that can change independently from WebSkrap.

Generate the headed and headless stealth report:

```bash
WEBSKRAP_LIVE=1 python scripts/live_stealth_report.py --no-open --report-only
```

PowerShell:

```powershell
$env:WEBSKRAP_LIVE=1
python scripts\live_stealth_report.py --no-open --report-only
```

Open `.webskrap/reports/live-stealth-results.html`. For proxy DNS checks, set
`WEBSKRAP_LIVE_EXPECTED_PUBLIC_IP` or `WEBSKRAP_LIVE_EXPECTED_COUNTRY`.

## With uv

The repository includes `uv.lock`, so you can run the same commands through
`uv` without activating a shell environment:

```bash
uv run pytest -q
uv run ruff check .
```

## Publishing docs

Docs are deployed by GitHub Actions from `main`.

Repository settings must use:

- Settings
- Pages
- Source: GitHub Actions
