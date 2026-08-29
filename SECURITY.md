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
symlink in between. WebSkrap creates its own default root `0700` so no other
account can plant anything there, which is as far as a local Python library can
reasonably close it. A root you point at a world-writable directory is outside
that guarantee.

`stealth_fetch` applies the same trust-boundary rule to persistent browser
profiles. Its `user_data_dir` is relative to `~/.webskrap/profiles` by default;
set `WEBSKRAP_MCP_PROFILE_DIR` to move that root. Absolute paths, `..`
traversal, the root itself, and symlinks resolving outside it are rejected.
This restriction applies only to MCP tool input. The Python API continues to
accept caller-chosen profile paths.

### Proxy credentials

`ProxyConfig` holds proxy usernames and passwords in memory and passes them to
Playwright. WebSkrap never writes them to disk, but they do appear in the
model's `repr` and `model_dump`, so keep them out of logs and error reports.

### What is out of scope

Bot-detection evasion is what this library does; a site detecting WebSkrap is
not a vulnerability. Neither is a scraped page containing malicious content,
as long as WebSkrap passes it through as data rather than executing it.
