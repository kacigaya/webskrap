# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries for releases before 0.9.1 are reconstructed from git tags and commit
history, so they summarize each release rather than list every change.

## [Unreleased]

### Security

- Persistent browser session names `.` and `..` are rejected. They previously
  resolved to the session root or its parent, so closing one with profile-data
  deletion enabled could remove files outside an individual session directory.
- Persistent browser commands reject a session directory that is a symlink,
  preventing a preplanted entry under a shared `WEBSKRAP_BROWSER_DIR` from
  redirecting profile and state-file operations outside the sessions root.
- MCP `stealth_fetch` profile paths are confined under
  `~/.webskrap/profiles` (override with `WEBSKRAP_MCP_PROFILE_DIR`). Absolute
  paths, traversal, and escaping symlinks are rejected before Chromium starts;
  unrestricted profile paths remain available through the Python API.

### Fixed

- Concurrent open and close operations for the same persistent browser session
  are serialized across processes. This prevents duplicate Chromium launches,
  stale state files, and a close racing with an in-progress open.
- `webskrap fetch --text-only --output` labels the saved artifact as text in
  its human summary instead of calling it HTML.

### Added

- Python 3.14 support and CI coverage.
- Dependabot configuration for the Python, GitHub Actions, and docs-site
  dependency trees, grouped so routine minor and patch bumps arrive as one
  weekly pull request and majors stay separate. `pip-audit` already failed CI
  on a known vulnerability; this is what opens the pull request that fixes it.

## [1.0.0] - 2026-08-22

First stable release. The public API is what `webskrap.__all__` exports plus
the documented CLI and MCP tool surfaces; from here breaking changes to those
require a major version. Nothing in the library changed from 0.10.1 beyond the
entries below.

### Security

- Permission tightening no longer follows symlinks. `secure_directory` used
  `Path.chmod`, which resolves the final component, so a symlink planted where
  a session directory belongs could have handed the mode change to its target.
  It now opens the directory with `O_NOFOLLOW` and uses `fchmod`, leaving a
  symlinked directory and its target untouched.
- The MCP output root is created `0700` when WebSkrap creates it, so no other
  local account can plant symlinks inside the default `./webskrap-output`. A
  root the user set up keeps its own permissions.
- Cursor jitter draws from `secrets.SystemRandom` instead of `random`. The
  values are pixel offsets and sleep durations, never secrets, but the change
  removes the last non-cryptographic RNG from the package, so no security
  scanner has to be told to ignore one.

### Added

- Bandit runs as an independent security gate (`bandit -c pyproject.toml -r
  src/`), in CI alongside Ruff's `S` rules. It is a second implementation of
  the same checks and reads the `# nosec` markers Ruff ignores. Every finding
  is suppressed at its own line with a reason; nothing is skipped globally, so
  the same rule still catches an unsafe use elsewhere.
- A dedicated `coverage` CI job that combines the data files from every Python
  version and the browser job, then enforces the 85% bar over the union. It
  depends on all test jobs, so the gate cannot pass on a partial run. Branch
  coverage is on.

### Changed

- Ruff's `D105` and `D107` are enforced: `D203` and `D213` are now the only
  ignored docstring rules. The affected magic methods and constructors describe
  lazy driver start, context ownership, and what leaving an `async with` block
  releases.
- CI separates its two requirements: Python 3.11/3.12/3.13 compatibility, and
  full-library coverage. Browsers install in one job rather than in every
  matrix entry.

### Fixed

- `resolve_output_path` rejects destinations that name no file (`.`, `""`)
  with a clear message rather than failing later inside the browser.

## [0.10.1] - 2026-08-22

### Fixed

- CLI usage-error tests asserted against Rich's wrapped panel output, so they
  passed locally and failed on CI's narrower terminal. Library behavior is
  unchanged; 0.10.0 never reached PyPI because the release workflow is gated
  on those tests.

## [0.10.0] - 2026-08-22

### Security

- Persistent browser sessions now keep Chromium's OS sandbox. Previously every
  session launched with `--no-sandbox`, so a renderer compromised by a hostile
  page had no containment. Opt out with `webskrap browser open --no-sandbox`,
  the `chromium_sandbox=False` argument to `open_session`/`launch_browser`, or
  `WEBSKRAP_CHROMIUM_SANDBOX=0` where the sandbox genuinely cannot start. A
  sandbox-related launch failure reports how to opt out; it never retries
  without the sandbox on its own.
- MCP screenshots are confined to an output root (`./webskrap-output` by
  default, `WEBSKRAP_OUTPUT_DIR` to move it). `browser_screenshot` previously
  wrote to any path a model asked for and created parent directories along the
  way. Absolute paths, `..` traversal, and symlinks leaving the root are now
  rejected before the browser is touched.
- Session directories (each session and its profile data) are created `0700`
  on POSIX and tightened if they already exist. They hold cookies and
  logged-in state that other local accounts could read. A sessions root the
  user set up themselves (`WEBSKRAP_BROWSER_DIR`) keeps its permissions, since
  it may be shared deliberately; WebSkrap only creates its own root `0700`.

### Added

- `webskrap.paths` with `resolve_output_path` and `secure_directory`.
- `browser_session.sandbox_enabled` and `create_session_dir`.
- `--no-sandbox` on `webskrap browser open`, plus a warning when a session
  starts without the sandbox.
- `chromium_sandbox` in a session's state file, in `browser open` output, and
  as a `Sandbox` column in `webskrap browser list` / `browser_list`, so an
  operator can see which running sessions gave up renderer isolation.
- `CHANGELOG.md`, `SECURITY.md`, and `CONTRIBUTING.md`.

### Changed

- Docstrings across the public API: client, sessions, models, parsing,
  profiles, and CLI commands.
- Ruff now enforces `S` (security) and `D` (docstring) rules, with narrow
  per-file exceptions instead of blanket ignores. Pyright's
  `typeCheckingMode` is pinned to `standard` in `pyproject.toml`.
- CI runs the suite on Python 3.11, 3.12 and 3.13, gates coverage at 85%, and
  checks package metadata with `twine check` before any PyPI upload.
- `pytest-cov` is part of the `dev` extra.

### Migration

- Sessions on hosts without a working Chromium sandbox (unprivileged
  containers, images with user namespaces disabled) now fail to open until you
  opt out with `--no-sandbox` or `WEBSKRAP_CHROMIUM_SANDBOX=0`. The error names
  both.
- MCP clients passing absolute screenshot paths must switch to relative ones,
  or set `WEBSKRAP_OUTPUT_DIR` to the directory they want written to.
- `webskrap browser screenshot` (the CLI) is unchanged: its path comes from the
  person running the command, not from a model.

## [0.9.1] - 2026-08-20

### Changed

- Trimmed the skill description and tightened prose across `SKILL.md` and the
  docs site.

## [0.9.0] - 2026-08-20

### Added

- Persistent browser sessions for the CLI (`webskrap browser ...`) and the MCP
  server: a detached Chromium that outlives each invocation, driven over CDP
  with aria-snapshot element refs.

### Fixed

- Overrode `nanoid` and `js-yaml` to patched versions in the docs site.

## [0.8.0] - 2026-07-30

### Changed

- Safer browser defaults.

### Added

- Mobile docs menu on the docs site.

### Fixed

- Kept the mobile docs menu inside the viewport.
- Pinned `setup-uv` to v9.0.0; publishing is gated on lint, tests and build.

## [0.7.1] - 2026-07-27

### Fixed

- `webskrap fetch` falls back to bundled Chromium when the requested channel
  cannot launch, and cookie-consent waits are capped.

## [0.7.0] - 2026-07-27

### Changed

- Relicensed to Apache-2.0.

### Added

- Automatic cookie-consent declining.

## [0.6.1] - 2026-07-01

### Removed

- Dead code and unused dependencies.

## [0.6.0] - 2026-07-01

### Changed

- Synced docs-site dependencies.

## [0.5.9] - 2026-07-01

### Changed

- The MCP `fetch` tool uses the stealth driver by default.

## [0.5.8] - 2026-07-01

### Changed

- MCP tools return clean page text by default, for LLM consumers.

## [0.5.7] - 2026-07-01

### Added

- Daily PyPI update-check notice in the CLI.

## [0.5.6] - 2026-07-01

### Added

- Benchmarks docs page and Patchright focus control.

## [0.5.5] - 2026-06-30

### Fixed

- Removed an unsupported Patchright focus option.

## [0.5.4] - 2026-06-30

### Added

- `webskrap install` covering both Playwright and Patchright browsers.

## [0.5.3] - 2026-06-30

### Added

- LLM-friendly CLI output.

## [0.5.2] - 2026-06-29

### Added

- Next.js showcase site and documentation.

## [0.5.1] - 2026-06-29

### Changed

- Tightened Patchright stealth defaults.

## [0.5.0] - 2026-06-26

Earlier releases (0.1.0 - 0.4.9) are recorded in the git history and tags only.

[Unreleased]: https://github.com/kacigaya/webskrap/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/kacigaya/webskrap/compare/v0.10.1...v1.0.0
[0.10.1]: https://github.com/kacigaya/webskrap/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/kacigaya/webskrap/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/kacigaya/webskrap/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/kacigaya/webskrap/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/kacigaya/webskrap/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/kacigaya/webskrap/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/kacigaya/webskrap/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/kacigaya/webskrap/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/kacigaya/webskrap/compare/v0.5.9...v0.6.0
[0.5.9]: https://github.com/kacigaya/webskrap/compare/v0.5.8...v0.5.9
[0.5.8]: https://github.com/kacigaya/webskrap/compare/v0.5.7...v0.5.8
[0.5.7]: https://github.com/kacigaya/webskrap/compare/v0.5.6...v0.5.7
[0.5.6]: https://github.com/kacigaya/webskrap/compare/v0.5.5...v0.5.6
[0.5.5]: https://github.com/kacigaya/webskrap/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/kacigaya/webskrap/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/kacigaya/webskrap/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/kacigaya/webskrap/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/kacigaya/webskrap/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/kacigaya/webskrap/releases/tag/v0.5.0
