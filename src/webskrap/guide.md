# WebSkrap guide

## Which tool

| Goal | Call |
| --- | --- |
| Find URLs for a topic | `search`, then `stealth_fetch` the hits worth reading |
| Read one page | `stealth_fetch` (preferred) or `fetch` |
| Read a page and follow its links | `stealth_fetch` with `include_links=true` |
| Read a long page | `stealth_fetch`, then repeat with `offset=next_text_offset` |
| Release a warm fetch session | `fetch_session_close` |
| Click, type, log in, multi-step flow | `browser_open` -> `browser_snapshot` -> `browser_interact` -> `browser_wait_for` |
| See and interact with visual targets | `browser_open` -> `browser_view` -> `browser_mouse` |
| Check why something failed | `doctor` |

`fetch` and `stealth_fetch` use the same stealth browser. `stealth_fetch` adds
fingerprint, WebRTC, user-agent and persistent-profile control, and is the one
to reach for by default. Without `session`, each call gets a fresh browser and
temporary profile. An explicit `user_data_dir` keeps its on-disk state.

For repeated extraction, pass `session="crawl"` to either fetch tool. The
browser stays warm and cookies/storage are shared within that name. Use the
same profile and config on every call; `timeout_ms`, `decline_cookies`, output
options and `wait_until` may vary. Other changes are rejected until you call
`fetch_session_close(session="crawl")`. At most eight fetches run at once and
eight named fetch sessions stay open. A busy session cannot be closed. These
sessions end when the MCP server stops and are separate from `browser_*`
sessions. Persistent `user_data_dir` files survive closing.

Both fetch tools accept `resource_policy` and `wait_until`. `stealth_fetch`
defaults to `domcontentloaded`; `fetch` defaults to `networkidle`. Use `lite`
to skip images/fonts/media or `documents` to also skip stylesheets. Earlier
load states can read before deferred page content arrives. Routing disables
the browser's HTTP cache and does not intercept service-worker-owned requests.

`search` loads Bing's results page (`engine="bing"`, the default) or
DuckDuckGo's HTML page (`engine="ddg"`) in the same stealth browser and
returns the organic hits with their click-tracking unwrapped. Google is not offered. Some exit
addresses get a bot challenge from DuckDuckGo; that is the `blocked` error, and
the answer is the other engine, a persistent `user_data_dir`, or another exit
IP, not a retry.

## A session, start to finish

1. `browser_open(url=...)` -- starts or reuses a detached headless Chromium.
2. `browser_snapshot()` -- returns the accessibility tree with `[ref=eN]` handles.
3. `browser_interact(action="fill", target="e12", values=["hello"])`.
4. `browser_wait_for(text="Welcome")` -- not another snapshot.
5. `browser_snapshot()` again, because the refs from step 2 are now stale.
6. `browser_close()` -- the profile survives, so the next open is still logged in.
   `delete_data=true` throws the cookies and logins away.

One page per session, no tabs, headless only over MCP.

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

## What each call returns

| Tool | Keys |
| --- | --- |
| `fetch`, `stealth_fetch` | `url`, `final_url`, `status`, `ok`, `title`, `headers`, `text`, `text_length`, `text_offset`, `text_truncated`, `next_text_offset`, `links`, `links_total`, `links_truncated`, `elapsed_ms`, `cookie_notice_declined` |
| `search` | `query`, `engine`, `url`, `final_url`, `status`, `ok`, `hits` (`title`, `url`, `snippet`), `hits_total`, `hits_truncated`, `elapsed_ms`, `cookie_notice_declined` |
| `browser_open` | `session`, `pid`, `port`, `reused`, `chromium_sandbox` |
| `browser_goto` | `status`, `url`, `title` |
| `browser_text` | `url`, `title`, `text`, `text_length`, `text_offset`, `text_truncated`, `next_text_offset` |
| `browser_snapshot` | `url`, `title`, `snapshot`, `snapshot_length`, `snapshot_offset`, `snapshot_truncated`, `next_snapshot_offset` |
| `browser_interact`, `browser_press`, `browser_mouse`, `browser_insert_text` | `url`, `title` |
| `browser_wait_for` | `url`, `title`, `matched` |
| `browser_eval` | `result`, `result_length`, `result_truncated`, and `result_json` when clipped |
| `browser_view` | Inline PNG plus `url`, `title`, `width`, `height`, `scroll_x`, `scroll_y`, `coordinate_system`, `mime_type` |
| `browser_screenshot` | `url`, `title`, `path` |
| `browser_close` | `closed` |
| `browser_list` | `sessions` |

## Cost

| Lever | Effect |
| --- | --- |
| `max_chars` | Defaults to 8000 characters. Use offsets to read more. |
| `offset` | Pages text or snapshots. Fetch tools navigate again; session text stays open. |
| `resource_policy="lite"` | Skips images, fonts and media. |
| `text_only=true` | Readable text instead of markup. On by default. |
| `depth` | Defaults to 6; use null for unlimited snapshot depth. |
| `include_links=false` | On by default; links cost more than the text on some pages. |
| `max_results` | A search returns 10 hits by default; `hits_total` says how many the page had. |

## When it fails

| Code | Do this |
| --- | --- |
| `no_session` | `browser_open` first. |
| `session_unreachable` | `browser_close`, then `browser_open`. |
| `stale_ref` | Snapshot again; refs belong to one snapshot. |
| `timeout` | Raise `timeout_ms`, weaken `wait_until`, or `browser_wait_for` first. |
| `navigation` | Check the URL and that the host resolves. |
| `browser_launch` | Run `webskrap install`; on Linux ARM64 pass `channel="chromium"`. |
| `sandbox` | Set `WEBSKRAP_CHROMIUM_SANDBOX=0` only where the sandbox cannot start. |
| `path_rejected` | Paths are relative to a confined root. |
| `blocked` | The site served a bot challenge. Switch engine or exit IP; no CAPTCHA solving. |
| `usage` | Re-read the argument's documented values. |

## Limits

Screenshots are written under `./webskrap-output` and persistent profiles under
`~/.webskrap/profiles` (`WEBSKRAP_OUTPUT_DIR` and `WEBSKRAP_MCP_PROFILE_DIR` move
those roots). Absolute paths and traversal are rejected. The sandbox switch is an
environment variable and never a tool argument, so a page cannot argue a model
into disabling it. No CAPTCHA solving and no login-wall bypass.
