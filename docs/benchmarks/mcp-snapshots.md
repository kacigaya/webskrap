# MCP snapshot workflow measurements

Measured on 2026-11-14 on Linux ARM64, Python 3.14.7, uv 0.13.5 and
Chrome for Testing 156.0.8078.47, headless with sandbox explicitly disabled.
Baseline revision: `914e24576ebe92e08d46c2867d065343b1b71fdc`.
The measurements precede the review fixes for inline-text search, URL escaping
and stacked dialogs. Those fixes have separate regression coverage; the
benchmark has not been rerun for them.
No production dependency was added.

## Method

The task page has 180 records, headings, descriptions and edit buttons. The
workflow locates record 179, opens its native modal editor, locates and fills
the Title field, locates Save changes, clicks it and checks the saved value.
This measures a three-interaction workflow, including DOM changes and modal
state, rather than an isolated snapshot. It is one synthetic task, not a
representative sample of the web or an actual model inference run.

Each WebSkrap mode runs three times in alternating order with a fresh browser.
Baseline loads the original CDP module from the revision above. Snapshot mode
uses the new code with default depth and character limits. Both page through
8,000-character responses until the named control is present. Find mode uses
literal name search. All modes use the same native WebSkrap interactions.

The Playwright comparison uses `@playwright/mcp@0.0.85` in npm's external cache,
the same Chromium executable and HTML, and the same names and actions. It
runs three times each with default automatic snapshots and with
`--snapshot-mode=none`. Both use `browser_find`. Playwright can save large snapshots to local files;
these counts cover returned text, not unrequested file contents. The task needs
only find results, so it does not read those files. Playwright loads a temporary
file; WebSkrap injects the document into about:blank. Loading and browser
startup are excluded. Verification reads are excluded from tool/token counts.

Token counts use tiktoken 0.14.0, `o200k_base`. Request counts encode JSON tool
names and arguments. Response counts encode each WebSkrap JSON payload once,
or Playwright's returned text content once. These are measured payload tokens,
not billed model input/output usage. They exclude JSON-RPC envelopes, duplicate
MCP text/structured serialization, tool schemas, model reasoning and replayed
conversation context. A client that sends both structured and text payloads to
a model can consume more tokens. No model was invoked to select actions.

Snapshot latency covers each read plus shaping; the reported number is the
median of each run's median. WebSkrap calls Python handlers directly;
Playwright uses MCP stdio, so cross-driver latency includes different transport
costs. Task time includes trusted interactions and verification. WebSkrap's
human cursor, scroll and typing timing differs from Playwright's interaction
implementation. Compare read latency separately from total task time.

CPU is the `/proc` CPU-time delta for Python plus its current descendants.
Memory is their summed RSS after the task, not peak memory or unique physical
memory; shared pages can be counted more than once. `/proc` sampling and
regex-worker lifecycle are approximate. Runs shared the host with tests.
Timing and memory are observations, not isolated performance guarantees.

## Results

All rows are medians over three successful runs. Each driver completed 3/3
workflows per mode with the expected saved value.

| Mode | Request tokens | Response tokens | Response bytes | Tool calls | Read ms | Task ms | CPU s | Summed RSS MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| WebSkrap baseline, paged snapshot | 245 | 32,636 | 101,972 | 15 | 189.53 | 14,903.85 | 4.12 | 1,160.08 |
| WebSkrap new, paged snapshot | 161 | 11,054 | 34,568 | 9 | 132.49 | 13,055.63 | 2.61 | 1,247.55 |
| WebSkrap new, targeted find | 123 | 302 | 974 | 6 | 11.52 | 12,361.84 | 1.96 | 1,244.19 |
| Playwright MCP, find + default snapshots | 91 | 479 | 1,508 | 6 | 90.02 | 2,248.47 | 0.73 | 1,335.70 |
| Playwright MCP, find + no automatic snapshots | 91 | 351 | 1,162 | 6 | 77.64 | 2,026.75 | 0.75 | 1,314.17 |

The new snapshot path saves tokens in this task mainly because it omits
background content behind the modal. It preserves the existing format for
ordinary nonmodal pages. Targeted find reduces response tokens by 99.1% against
the baseline workflow and uses six calls instead of fifteen. This reduction is
specific to a known target near the end of a long page. Small pages or tasks
that need broad context will yield different results.

Targeted find makes seven `DOM.describeNode` calls per task, compared with
8,688 in the baseline and 2,894 for the new paged snapshots. All WebSkrap modes
made zero `Runtime.enable`, `Console.enable`, `Accessibility.enable` or
`Accessibility.getFullAXTree` calls during the measured task. This is a CDP
regression indicator, not evidence that a protected website accepts the browser.
No new public anti-bot acceptance test was run.

Playwright completes trusted interactions faster in this task. WebSkrap's
read-only find is faster in these samples, but its total task is slower.
Summed RSS is higher than the old baseline in these samples; this method does
not establish a memory improvement. Literal find avoids regex subprocesses;
caller-supplied regex still pays worker startup and timeout protection costs.

The server instruction text is 656 tokens versus 658 before this change.
The new find tool definition is 425 tokens when encoded as its full MCP tool
record. A client that includes all tool schemas in each model prompt pays
that extra context cost, which is excluded from the task table.

## Reproduce

1. Run `git show 914e24576ebe92e08d46c2867d065343b1b71fdc:src/webskrap/cdp_dom.py > /tmp/webskrap-snapshot-baseline.py`.
2. Run `uv run --with tiktoken==0.14.0 python scripts/benchmark_snapshots.py --baseline /tmp/webskrap-snapshot-baseline.py`.
3. For the optional external comparison, run `uv run --with tiktoken==0.14.0 python scripts/benchmark_playwright_snapshots.py`.

The first script accepts `--repeats`. The second requires npx and installs only
into its external cache. Neither adds a WebSkrap runtime dependency. Both
explicitly disable Chromium's sandbox for this host. Use a sandbox on hosts
that support it. Load only a trusted baseline source file.

Raw measurements: [WebSkrap](mcp-snapshots-webskrap.json) and
[Playwright MCP](mcp-snapshots-playwright.json). Implementation decisions and
semantic limits are in [the design note](../design/mcp-snapshots.md).

## Validation and review

The complete local non-live suite passed: 966 tests, 30 live tests deselected,
88.20% coverage. Review then found that the existing frame-navigation error
handler could swallow an iframe regex-worker timeout. The handler now propagates
search usage/internal errors. The final focused run passed 47 tests with appended
coverage of 88.34%, including an independent regression test for that error path.

Final checks passed: Ruff lint and format, Pyright 1.1.411 for Linux, Windows
and Darwin, Bandit, source/wheel build and Twine metadata validation. The review
was performed locally. Public live tests and Windows/macOS runtime tests were
not run on this Linux host. Cross-platform typing does not replace those tests.

The lockfile pins `opentelemetry-api==1.45.2`, which fixes
[GHSA-mf5v-w3w3-f6cg](https://osv.dev/vulnerability/GHSA-mf5v-w3w3-f6cg)
(CVE-2026-126360, traceparent parsing denial of service). The previous locked
version, 1.45.1, failed the CI audit. Only that package's version and artifact
metadata changed. Both pip-audit and the OSV scan of 51 locked public packages
now report no known vulnerabilities. No new dependency was added.

After the audit repair, the full local non-live suite passed 967 tests with
89.15% coverage. Windows CI also exposed a fixture race: challenge detection
can return before an iframe's image request reaches the proxy. The local test
now defers that request and waits for its actual arrival with a five-second
deadline. The old immediate assertion failed against the delayed fixture;
all six transition/authentication cases pass with the bounded wait. The final
combined frame/snapshot run passed 30 tests. Production challenge handling is
unchanged.

Review follow-up validation: the full non-live run passed 980 tests with 89.15%
coverage. The final focused run passed 31 tests, including additional Unicode
separator cases. The broader session/MCP regression run passed 81 tests. New
regressions cover inline paragraph/status/value searches, ref-like visible text,
URL and ARIA line injection, and stacked modal opening/closing order. Ruff,
three-platform Pyright, Bandit, pip-audit, OSV, build and Twine passed. Public
live tests were excluded; local cross-platform runtime tests remain delegated
to CI. No benchmark numbers were regenerated for this follow-up.
