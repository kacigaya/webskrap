# MCP search and fetch check, 2026-11-03

The connected MCP server ran WebSkrap 3.2.0 on Python 3.11.15 and Chrome 156.
This checkout was version 3.2.4. The sample used 15 search and fetch calls,
plus one `doctor` call. Topics included animals, history, Python, and astronomy.
Fetch targets included documentation, Wikipedia, an RFC, redirects, and HTTP
error responses. This was an ad hoc sample, not a full web compatibility test.

The [sanitized log](mcp-web-smoke-2026-11-03.json) records each request and result
summary. It includes all four tool errors from the sample. It excludes raw HTML,
response headers, session identifiers, and search hit contents.

## Errors and findings

1. `fetch` on `https://httpbin.org/status/404` raised `navigation` with
   `net::ERR_HTTP_RESPONSE_CODE_FAILURE`.
2. `stealth_fetch` on `https://httpbin.org/status/500` raised the same error.
   Both responses had empty bodies. Chromium returned a document response,
   then loaded its own error page. The driver raised before reading that response.
   The shared CDP fix accepts this signal only with a matching document loader
   and an HTTP status from 400 through 599. Fetch preserves the response URL
   when Chromium reports `chrome-error://chromewebdata/` as the page location.
3. DuckDuckGo returned `blocked` for `Roman concrete volcanic ash`.
   The error correctly reported a bot challenge. No CAPTCHA interaction occurred.
   This remains an external limit.
4. `https://no-such-host.webskrap.invalid/` returned `navigation` with
   `net::ERR_NAME_NOT_RESOLVED`. This was an intentional failure check.
   The fix preserves this error.
5. Bing returned weak or unrelated hits for the sample queries. A raw HTML fetch
   for the octopus query also contained unrelated result headings while its query
   input held the requested text. No parser defect was confirmed. Search relevance
   remains unresolved; callers must assess the returned hits.

All other fetches returned content. The Python.org missing page returned HTTP
404 with `ok: false` before the fix because its error body was not empty.
The httpbin redirect reached `/get` with HTTP 200. Text windows and bounded
link output worked on the sampled documentation pages.

## Verification and limits

Local retests used the actual MCP protocol against this checkout's server,
with `WEBSKRAP_CHROMIUM_SANDBOX=0` for this host. The public httpbin 404 and
500 requests returned their expected status, `ok: false`, and HTTP URL.
The invalid hostname still returned `navigation`. The JSON log includes these
three retests. The connected 3.2.0 server was not replaced.

Deterministic tests cover both MCP fetch tools with empty 404 and 500 responses.
Driver tests cover DNS and TLS failures, a missing response, a missing loader,
and an HTTP failure signal with a successful status. Existing challenge detection
tests remain in place.

For an empty HTTP error body, returned text or HTML can be Chromium's generated
error page. It is not the origin's zero-byte body. Persistent browser navigation
still reports the browser's actual location. Fetch reports the HTTP response URL.

The first dependency-scan command used an unavailable `python` executable.
It was rerun with `uv run python` and passed. OSV found no advisories for the
51 locked public packages. `pip-audit` also found no known vulnerabilities.
Neither result guarantees that no unknown vulnerability exists.

Final local validation passed:

1. The full non-live suite: 904 passed, 30 deselected, 88.88% coverage.
2. The unit suite: 785 passed, 149 deselected.
3. Ruff lint and format checks, plus Bandit.
4. Pyright with Linux, Windows, and macOS platform targets.
5. OSV and `pip-audit` dependency checks.
6. Source and wheel builds, plus Twine metadata checks.

Runtime tests used this Linux host and its Python interpreter. Other operating
systems and Python versions were not run locally. The 30 live tests were excluded;
the public requests recorded here were run separately through MCP.
