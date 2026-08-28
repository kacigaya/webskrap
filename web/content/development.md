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
pytest -q -m "not browser and not live"
ruff check .
ruff format --check .
```

Before opening a pull request, add the browser tests and the coverage gate CI
enforces:

```bash
pytest -q -m "not live" --cov=webskrap --cov-report=term-missing --cov-fail-under=85
```

CI runs the suite on Python 3.11, 3.12 and 3.13, type-checks `src/webskrap`
with Pyright in `standard` mode, audits dependencies with `pip-audit`, and
validates package metadata with `twine check` before any release upload.
[CONTRIBUTING.md](https://github.com/kacigaya/webskrap/blob/main/CONTRIBUTING.md)
has the full list.

Hosts that cannot start Chromium's sandbox (unprivileged containers, images
without user namespaces) still run the session tests: the suite probes sandbox
support once and falls back to `WEBSKRAP_CHROMIUM_SANDBOX=0` for those tests.

Build the web docs:

```bash
cd web
bun install --frozen-lockfile
bun audit
bun run lint
NEXT_PUBLIC_BASE_PATH=/webskrap bun run build
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
