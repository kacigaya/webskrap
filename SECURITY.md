# Security Policy

## Supported versions

WebSkrap is developed on `main` and released from tags. Fixes land in a new
patch release on top of the latest minor; older minors are not backported.

| Version | Supported |
| ------- | --------- |
| 1.0.x   | Yes       |
| < 1.0   | No        |

## Reporting a vulnerability

Use GitHub's private vulnerability reporting, which is enabled on this
repository: open
<https://github.com/kacigaya/webskrap/security/advisories/new> (or press
**Report a vulnerability** on the repository's Security tab) to file a private
draft advisory. The report stays private to you and the maintainer until a fix
ships, and the advisory becomes the public record afterwards.

If you cannot use that form, email
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
Whatever they return crosses from the page into your program. Prefer
snapshots, interaction, and waits over eval, and never evaluate text copied
from a page. Over MCP, expressions are capped at 10,000 characters, logged
server-side, and disabled entirely with `WEBSKRAP_ALLOW_EVAL=0`.

### Fetch targets

One-shot and persistent navigation accept `http`/`https` URLs (`data:` pages
and `about:blank` carry no network request and stay allowed). Embedded
`user:pass@` credentials are rejected everywhere so they never reach logs or
state files.

The MCP server additionally refuses hosts that resolve to non-public
addresses (loopback, RFC1918, link-local, and friends), since its URLs arrive
from a model that reads untrusted pages. Set `WEBSKRAP_ALLOW_PRIVATE_NET=1`
to permit them. The Python API and CLI take URLs from the operator and skip
the DNS check; point them at internal hosts deliberately, not with
page-derived input.

### Chromium sandbox

Persistent sessions keep Chromium's OS sandbox. That sandbox is what stops a
renderer compromised by a hostile page from reaching the rest of the machine.

`--no-sandbox`, `chromium_sandbox=False`, and `WEBSKRAP_CHROMIUM_SANDBOX=0`
remove it. Use them only where the sandbox cannot start (unprivileged
containers, images with user namespaces disabled), and prefer fixing the host:
enable unprivileged user namespaces, or run the browser as a non-root user
with the sandbox intact. WebSkrap never drops the sandbox on its own, and
never retries a failed launch without it.

One-shot `fetch`/`search` calls keep the sandbox by default, the same as
persistent sessions. Opt out per call with `webskrap fetch --no-sandbox` /
`webskrap search --no-sandbox` or `chromium_sandbox=False`, or per host with
`WEBSKRAP_CHROMIUM_SANDBOX=0`. Sandbox-weakening flags are rejected when
passed through `--launch-arg`: use the explicit switch so the choice stays
visible.

### Persistent session state

`~/.webskrap/browser/<session>/` holds a full browser profile: cookies, local
storage, and any logged-in session you established. WebSkrap creates these
directories `0700` on POSIX, but that only keeps out other local accounts. The
data is not encrypted at rest, it survives process exit, and
`webskrap browser close --delete-data` is what removes it.

Session directories cannot be symlinks. WebSkrap rejects a session whose name
already points to one, without changing or deleting the link's target. This is
especially important when `WEBSKRAP_BROWSER_DIR` points to a shared root where
another local account could preplant a directory entry.

On Windows, POSIX mode bits do not apply; the profile inherits the ACLs of
your user directory.

### MCP file output

The MCP server is driven by a model that reads untrusted pages, so its file
destinations are untrusted input. `browser_screenshot` writes only under
`./webskrap-output` (or `WEBSKRAP_OUTPUT_DIR`). Absolute paths, `..`
traversal, and symlinks pointing outside that root are rejected.

Point `WEBSKRAP_OUTPUT_DIR` at a directory you are willing to have written to,
and do not set it to a source tree, a config directory, or `$HOME`.

Confinement is checked when the path is resolved, and the browser writes the
file a moment later. On a host where another local account can write inside the
output root, that gap is a race: a directory component could be replaced with a
symlink in between. WebSkrap narrows it by creating intermediate directories
`0700` itself, refusing to walk through symlinks, and re-resolving the
destination immediately before returning it -- and it creates its own default
root `0700` so no other account can plant anything there, which is as far as a
local Python library can reasonably close it. A root you point at a
world-writable directory is outside that guarantee.

`stealth_fetch` applies the same trust-boundary rule to persistent browser
profiles. Its `user_data_dir` is relative to `~/.webskrap/profiles` by default;
set `WEBSKRAP_MCP_PROFILE_DIR` to move that root. Absolute paths, `..`
traversal, the root itself, and symlinks resolving outside it are rejected.
This restriction applies only to MCP tool input. The Python API continues to
accept caller-chosen profile paths.

### Proxy credentials

`ProxyConfig` holds proxy usernames and passwords in memory and passes them to
Playwright. WebSkrap never writes them to disk. Its `repr`/`str` redact them,
error text scrubs embedded `user:pass@` authorities before it reaches a tool
result or terminal, and shaped CLI/MCP fetch payloads carry an allowlist of
response headers (content metadata, never `set-cookie` or credentials), so
keep the raw `FetchResult` out of logs regardless.

### What is out of scope

Bot-detection evasion is what this library does; a site detecting WebSkrap is
not a vulnerability. Neither is a scraped page containing malicious content,
as long as WebSkrap passes it through as data rather than executing it.
