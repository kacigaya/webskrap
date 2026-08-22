# Security Policy

## Supported versions

WebSkrap is developed on `main` and released from tags. Fixes land in a new
patch release on top of the latest minor; older minors are not backported.

| Version | Supported |
| ------- | --------- |
| 0.9.x   | Yes       |
| < 0.9   | No        |

## Reporting a vulnerability

Report privately through GitHub: open
<https://github.com/kacigaya/webskrap/security/advisories/new> to file a draft
security advisory. If that form is unavailable, email
`163902005+kacigaya@users.noreply.github.com` with `SECURITY` in the subject.

Please do not open a public issue, pull request, or discussion for an
unpatched vulnerability. Public reports expose users before a fix exists.

Include what you have: affected version or commit, the configuration in use
(CLI, Python API, or MCP server), reproduction steps, and the impact you
believe it has. A proof of concept helps, but a clear description is enough to
start.

Expect an acknowledgement within a week. Once a fix is ready it ships in a
patch release, and the advisory credits you unless you ask otherwise.

## Security model

WebSkrap drives a real browser over pages it does not control. Treat every
page as hostile input and everything below as part of your threat model.

### Untrusted pages

Page content reaches your process as text, HTML, cookies, and aria snapshots.
None of it is sanitized: it is scraped data, not trusted data. Do not feed it
into a shell, a template, a database query, or an LLM prompt without treating
it as attacker-controlled.

`browser_eval` and `webskrap browser eval` evaluate JavaScript in the page.
Whatever they return crosses from the page into your program.

### Chromium sandbox

Persistent sessions keep Chromium's OS sandbox. That sandbox is what stops a
renderer compromised by a hostile page from reaching the rest of the machine.

`--no-sandbox`, `chromium_sandbox=False`, and `WEBSKRAP_CHROMIUM_SANDBOX=0`
remove it. Use them only where the sandbox cannot start (unprivileged
containers, images with user namespaces disabled), and prefer fixing the host:
enable unprivileged user namespaces, or run the browser as a non-root user
with the sandbox intact. WebSkrap never drops the sandbox on its own, and
never retries a failed launch without it.

Note that one-shot `fetch` calls go through Playwright's own launcher, which
defaults to `chromium_sandbox=False`. Pass `launch_args` or run under a
hardened container if that matters for your use.

### Persistent session state

`~/.webskrap/browser/<session>/` holds a full browser profile: cookies, local
storage, and any logged-in session you established. WebSkrap creates these
directories `0700` on POSIX, but that only keeps out other local accounts. The
data is not encrypted at rest, it survives process exit, and
`webskrap browser close --delete-data` is what removes it.

On Windows, POSIX mode bits do not apply; the profile inherits the ACLs of
your user directory.

### MCP file output

The MCP server is driven by a model that reads untrusted pages, so its file
destinations are untrusted input. `browser_screenshot` writes only under
`./webskrap-output` (or `WEBSKRAP_OUTPUT_DIR`). Absolute paths, `..`
traversal, and symlinks pointing outside that root are rejected.

Point `WEBSKRAP_OUTPUT_DIR` at a directory you are willing to have written to,
and do not set it to a source tree, a config directory, or `$HOME`.

### Proxy credentials

`ProxyConfig` holds proxy usernames and passwords in memory and passes them to
Playwright. WebSkrap never writes them to disk, but they do appear in the
model's `repr` and `model_dump`, so keep them out of logs and error reports.

### What is out of scope

Bot-detection evasion is what this library does; a site detecting WebSkrap is
not a vulnerability. Neither is a scraped page containing malicious content,
as long as WebSkrap passes it through as data rather than executing it.
