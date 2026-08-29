# WebSkrap guide

## Which tool

| Goal | Call |
| --- | --- |
| Read one page | `stealth_fetch` (preferred) or `fetch` |
| Read a page and follow its links | `stealth_fetch` with `include_links=true` |
| Read a long page | `stealth_fetch`, then repeat with `offset=next_text_offset` |
| Click, type, log in, multi-step flow | `browser_open` -> `browser_snapshot` -> `browser_interact` -> `browser_wait_for` |
| Check why something failed | `doctor` |

`fetch` and `stealth_fetch` use the same stealth browser. `stealth_fetch` adds
fingerprint, WebRTC, user-agent and persistent-profile control, and is the one
to reach for by default. Neither keeps cookies between calls: that is what a
browser session is for.

There is no search tool. WebSkrap loads URLs; finding them is somebody else's job.

## A session, start to finish

1. `browser_open(url=...)` -- starts or reuses a detached headless Chromium.
2. `browser_snapshot()` -- returns the accessibility tree with `[ref=eN]` handles.
3. `browser_interact(action="fill", target="e12", values=["hello"])`.
4. `browser_wait_for(text="Welcome")` -- not another snapshot.
5. `browser_snapshot()` again, because the refs from step 2 are now stale.
6. `browser_close()` -- the profile survives, so the next open is still logged in.
   `delete_data=true` throws the cookies and logins away.

One page per session, no tabs, headless only over MCP.

## What each call returns

| Tool | Keys |
| --- | --- |
| `fetch`, `stealth_fetch` | `url`, `final_url`, `status`, `ok`, `title`, `headers`, `text`, `text_length`, `text_offset`, `text_truncated`, `next_text_offset`, `links`, `links_total`, `links_truncated`, `elapsed_ms`, `cookie_notice_declined` |
| `browser_open` | `session`, `pid`, `port`, `reused`, `chromium_sandbox` |
| `browser_goto` | `status`, `url`, `title` |
| `browser_snapshot` | `url`, `title`, `snapshot`, `snapshot_length`, `snapshot_offset`, `snapshot_truncated`, `next_snapshot_offset` |
| `browser_interact`, `browser_press` | `url`, `title` |
| `browser_wait_for` | `url`, `title`, `matched` |
| `browser_eval` | `result`, `result_length`, `result_truncated`, and `result_json` when clipped |
| `browser_screenshot` | `url`, `title`, `path` |
| `browser_close` | `closed` |
| `browser_list` | `sessions` |

## Cost

| Lever | Effect |
| --- | --- |
| `max_chars` | Defaults to 20000 characters, about 5k tokens. Start lower. |
| `offset` | Reads the rest instead of re-fetching with a bigger limit. |
| `resource_policy="lite"` | Skips images, fonts and media. |
| `text_only=true` | Readable text instead of markup. On by default. |
| `depth` | Shallower snapshot beats a clipped deep one. |
| `include_links=false` | On by default; links cost more than the text on some pages. |

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
| `usage` | Re-read the argument's documented values. |

## Limits

Screenshots are written under `./webskrap-output` and persistent profiles under
`~/.webskrap/profiles` (`WEBSKRAP_OUTPUT_DIR` and `WEBSKRAP_MCP_PROFILE_DIR` move
those roots). Absolute paths and traversal are rejected. The sandbox switch is an
environment variable and never a tool argument, so a page cannot argue a model
into disabling it. No CAPTCHA solving and no login-wall bypass.
