# Contributing

Thanks for looking at WebSkrap. This file covers the setup, the checks, and
what a reviewable pull request looks like.

## Requirements

- Python 3.11, 3.12, or 3.13 (CI runs all three).
- [uv](https://docs.astral.sh/uv/) for environments and dependencies.
- A Chromium build for browser tests, installed via `webskrap install`.

## Setup

```bash
git clone https://github.com/kacigaya/webskrap && cd webskrap
uv sync --extra dev
uv run webskrap install    # downloads Playwright and Patchright Chromium
uv run webskrap doctor     # confirms a browser actually launches
```

## Checks

Run what CI runs:

```bash
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uvx --from pyright==1.1.411 pyright --pythonpath .venv/bin/python src/webskrap
uv run --extra dev pytest -q -m "not browser and not live"
```

Before opening a pull request, add the browser tests and the coverage gate:

```bash
uv run --extra dev pytest -q -m "not live" \
  --cov=webskrap --cov-report=term-missing --cov-fail-under=85
```

CI measures coverage differently: each Python version runs the unit tests, the
browser job runs the browser tests, and a `coverage` job combines every data
file and enforces the 85% bar over the union. That job needs all the test jobs,
so the gate cannot pass on a partial run. To reproduce it locally:

```bash
COVERAGE_FILE=.coverage.unit uv run --extra dev pytest -q -m "not browser and not live" --cov
COVERAGE_FILE=.coverage.browser uv run --extra dev pytest -q -m "browser and not live" --cov
uv run --extra dev coverage combine
uv run --extra dev coverage report --fail-under=85
```

Security and packaging checks:

```bash
uv run --extra dev bandit -c pyproject.toml -q -r src/
uv run --extra dev --with pip-audit pip-audit
uv build && uv run --with twine twine check dist/*
```

`ruff format .` fixes formatting; `ruff check . --fix` fixes what it safely
can. Ruff enforces security (`S`) and docstring (`D`) rules on `src/`, and
Bandit runs the same class of checks independently. If a finding is a false
positive, suppress that one line and say why — Ruff reads `# noqa: <rule>`,
Bandit reads `# nosec <id>`, and both fit on one line:

```python
import subprocess  # nosec B404  # noqa: S404 - fixed argv, never a shell
```

Never skip a rule globally or for a whole file: a line-scoped suppression still
lets the same rule catch an unsafe use somewhere else.

## Test markers

| Marker | What it means | How to run |
| ------ | ------------- | ---------- |
| (none) | Unit tests, no browser, no network | `pytest -m "not browser and not live"` |
| `browser` | Launches a local Chromium | `pytest -m browser` |
| `live` | Hits public bot-detection demos | `WEBSKRAP_LIVE=1 pytest -m live` |

Live tests are opt-in because they depend on third-party sites; they are not
part of CI and a failure there usually says more about the site than about
your change. Browser tests need `webskrap install` to have run.

Hosts without a working Chromium sandbox (unprivileged containers, images with
user namespaces disabled) are handled by the `persistent_session_env` fixture,
which probes once and falls back to `WEBSKRAP_CHROMIUM_SANDBOX=0` for session
tests. Do not hardcode that variable in new tests.

New tests should be deterministic and independent: no shared mutable state, no
ordering assumptions, no sleeping on wall-clock time. Use `parametrize` for
input matrices, and assert against the public API rather than private
attributes.

## Pull requests

- One topic per pull request. Unrelated cleanups belong in their own.
- Describe what changed and why. Link the issue if there is one.
- Add or adjust tests for changed behavior, especially parsing, path handling,
  session state, and anything security-relevant.
- Keep the public API stable. New parameters should be keyword-only and typed,
  and exported names go in `__all__`.
- Update docstrings and the docs site when behavior changes. Public functions,
  classes, and modules need docstrings — Ruff enforces this.
- Make sure the checks above pass locally. CI runs them on three interpreters.

## Changelog

Add an entry to the `Unreleased` section of `CHANGELOG.md` for anything a user
would notice: behavior changes, new options, fixes, and especially security
changes. Follow the existing Keep a Changelog headings (`Added`, `Changed`,
`Fixed`, `Removed`, `Security`). Internal refactors that change nothing
observable do not need one.

Breaking changes need a `Migration` note saying what callers must do.

## Security

Do not report vulnerabilities through issues or pull requests. Follow
[SECURITY.md](SECURITY.md) for private disclosure.
