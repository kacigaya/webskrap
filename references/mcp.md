# MCP

Read this reference for MCP tools, resources, schemas, annotations, and safety
boundaries. Confirm the current contract in `src/webskrap/mcp_server.py` and
`tests/test_mcp.py`.

## Select a tool

- `stealth_fetch` is the default one-shot fetch. It exposes fingerprint,
  WebRTC, user-agent, and persistent-profile controls.
- `fetch` is the simpler one-shot form.
- `search` finds URLs for a query on Bing (default) or DuckDuckGo, with the
  same stealth controls as `stealth_fetch`. Google is not offered. Follow up
  with `stealth_fetch` on the hits worth reading.
- `doctor` reports readiness, browser identity, CPU architecture, Fontconfig
  family count, versions, paths, environment overrides, persistent sessions,
  and the host timezone, with a warning when it is UTC.
- `browser_open` and the other `browser_*` tools drive a stateful flow.
  `browser_open` takes `webrtc_ip_handling_policy` but no proxy; open a proxied
  session with `webskrap browser open --proxy URL` and the tools reuse it.
- `webskrap://guide`, `webskrap://profiles`, and `webskrap://sessions` expose
  static guidance and current state as resources.

One-shot fetches do not share cookies between calls. Use a browser session when
state must persist.

## Keep results small

- `max_chars` defaults to 8000 for fetch, snapshot, text, and eval tools.
  Continue with `offset` and `next_text_offset` on stable page text. Fetch
  windows navigate again; use `browser_text` in a session to avoid re-fetching.
- `browser_text` reads visible body text and selects the window in the browser,
  avoiding a full accessibility tree and full-text transfer. The browser still
  computes full body text to report its length.
- Use `resource_policy=lite` to skip images, fonts, and media.
- Keep `include_links` off unless links are needed.
- Snapshots default to `depth=6`. Set `depth=null` for an unlimited tree,
  or reduce depth before increasing `max_chars`.
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

Fetch and navigation URLs must be public `http(s)` without credentials.
Private or local hosts are rejected unless `WEBSKRAP_ALLOW_PRIVATE_NET=1`.

`browser_eval` runs page script in a possibly logged-in profile: prefer
snapshot, interact, and wait_for, never evaluate page-controlled text, and set
`WEBSKRAP_ALLOW_EVAL=0` where models must not run page script.

## Failures

A known DataDome or Cloudflare challenge arrives as a `blocked` tool error,
including challenges served with HTTP 200. Ordinary HTTP error pages return
normally with `ok: false`. A raised failure includes `code` and `hint`. Read
`src/webskrap/errors.py` for the current catalog rather than duplicating it.

Both fetch tools accept `ready_selector`, a Playwright selector that must be
visible after consent dismissal. The wait uses `timeout_ms` separately from
navigation; a missing selector returns a `timeout` tool error. It may vary between
calls in a warm session. These checks do not establish that later interactions
will remain free of challenges.

## Vision and coordinate input

`browser_view(session="visual")` returns an inline `image/png` image block,
a text block with metadata, and the same metadata in `structuredContent`:
`url`, `title`, `width`, `height`, `scroll_x`, `scroll_y`, `mime_type`, and
`coordinate_system: "viewport-css-pixels"`. It writes no file. Use it with an
image-capable model and client; `browser_screenshot` remains the file/export tool.

Open with `browser_open(session="visual", url=...)`, inspect
`browser_view(session="visual")`, then call:

```json
{"action": "click", "x": 120, "y": 80, "session": "visual"}
```

with `browser_mouse`. Choose coordinates from the observed target. For a focused
input, `browser_insert_text(text="hello 世界", session="visual")` inserts literal
text; `browser_press(key="Enter", session="visual")` sends a key. Insertion emits
an input event, not individual keydown/keyup events. Use `browser_interact` with
`action="type"` when a control requires individual keystrokes.

Mouse actions are `click`, `dblclick`, `move`, `scroll`, and `drag`. All require
`x` and `y`. `button` is `left` (default), `middle`, or `right`, for click,
double-click, or drag only. Drag also requires `end_x` and `end_y`. Scroll accepts
`delta_x` and `delta_y` in CSS pixels, targeting the element under the pointer;
positive `delta_y` scrolls down. Invalid, non-finite, unused, or out-of-viewport
arguments return a `usage` error before mouse input is sent.

Use the original PNG dimensions: each image pixel is one CSS pixel, independent
of device scale. `(0,0)` is the viewport's top-left. Map any resized preview back
to `width`/`height`, and do not add scroll offsets. Full-page screenshots do not
share this mouse coordinate mapping.

Prefer refs/selectors for identifiable controls. Coordinate clicks hit whatever
is visible at the point. After an action, wait for the expected change, then take
a fresh view before choosing more coordinates. Wheel input can finish after the
call returns. Run dependent actions sequentially in each session. The image
shows the page, including canvas and frames, not browser chrome, OS dialogs,
or a live desktop stream. Headless rendering needs no Xvfb.
