# MCP

Read this reference for MCP tools, resources, schemas, annotations, and safety
boundaries. Confirm the current contract in `src/webskrap/mcp_server.py` and
`tests/test_mcp.py`.

## Select a tool

- `stealth_fetch` is the default one-shot fetch. It exposes fingerprint,
  WebRTC, user-agent, and persistent-profile controls.
- `fetch` is the simpler one-shot form.
- `doctor` reports readiness, versions, paths, environment overrides, and
  persistent sessions.
- `browser_open` and the other `browser_*` tools drive a stateful flow.
- `webskrap://guide`, `webskrap://profiles`, and `webskrap://sessions` expose
  static guidance and current state as resources.

One-shot fetches do not share cookies between calls. Use a browser session when
state must persist.

## Keep results small

- Start with a low `max_chars`. Continue with `offset` and
  `next_text_offset` instead of fetching again.
- Use `resource_policy=lite` to skip images, fonts, and media.
- Keep `include_links` off unless links are needed.
- For snapshots, reduce `depth` before truncating a deep tree.
- Make `browser_eval` return the required value, not the full DOM.

## Safety boundaries

Every tool declares title, read-only, destructive, idempotent, and open-world
annotations. Mark interactions as destructive because clicks and key presses
can submit forms on external sites.

Keep short operational instructions in the MCP server and long guidance in
`src/webskrap/guide.md`.

`stealth_fetch.user_data_dir` is relative to the MCP profile root, which
defaults to `~/.webskrap/profiles` and can be moved with
`WEBSKRAP_MCP_PROFILE_DIR`. Tool input must not select an absolute path or
escape that root.

`browser_screenshot` writes below `./webskrap-output`, unless
`WEBSKRAP_OUTPUT_DIR` changes the root. Reject absolute paths, traversal, and
symlinks that leave it.

The sandbox opt-out is an environment variable, never an MCP argument. Page
content must not be able to persuade the client to disable the browser sandbox.

## Failures

A fetch with an HTTP error status returns normally with `ok: false`. A raised
failure includes `code` and `hint` and arrives as an MCP tool error. Read
`src/webskrap/errors.py` for the current catalog rather than duplicating it.
