# Migrate from WebSkrap 2.x to 3.0

WebSkrap 3.0 drives Chromium only through its own Chrome DevTools Protocol
(CDP) client. Playwright and Patchright are no longer dependencies. Most code
that uses `client.fetch()`, `client.search()`, the CLI or the MCP server works
unchanged. Code that used Playwright pages or driver options must change.

## Steps

1. Upgrade the package: `pip install -U webskrap`.
2. Download the browser: `webskrap install`. It installs Chrome for Testing in
   the user cache folder. Set `WEBSKRAP_BROWSERS_PATH` to use another folder.
3. Remove the `SessionConfig` fields in the table below. WebSkrap 3.0 rejects
   them with an error that names the replacement.
4. Replace Playwright page code with `CDPPage` calls. See
   [Pages](#pages).
5. Remove `--driver` and `--patchright-context-profile` from CLI calls, and
   `driver` and `patchright_context_profile` from MCP tool calls.

## Removed configuration

| 2.x | 3.0 |
| --- | --- |
| `SessionConfig(driver=...)` | Remove it. There is one driver. |
| `SessionConfig(browser="firefox" \| "webkit")` | Not supported. WebSkrap drives Chromium only. |
| `SessionConfig(slow_mo_ms=...)` | Add your own delays between actions. |
| `SessionConfig(patchright_focus_control=...)` | Remove it. |
| `SessionConfig(patchright_context_profile=True)` | `SessionConfig(profile_mode="locale")` |
| Playwright default (profile applied in full) | `SessionConfig(profile_mode="full")` |
| `--patchright-context-profile` | `--profile-mode locale` |
| `--driver cdp`, `driver="cdp"` | Remove it. CDP is the only driver. |
| `webskrap.cdp_fetch.fetch()` | `WebSkrapClient().fetch()` |
| `client.start()` | Remove it. The browser starts on first use. |

`profile_mode` has three values:

- `"native"` (default): pages see the browser's own identity.
- `"locale"`: locale, timezone, color scheme, reduced motion and extra headers.
- `"full"`: also viewport, screen, device scale, touch and user agent.

## Pages

`WebSkrapSession.context` is gone. A session owns one `CDPBrowser` in
`session.browser`. Open pages with `session.new_page()`.

| 2.x (Playwright) | 3.0 (CDP) |
| --- | --- |
| `page = await session.context.new_page()` | `page = await session.new_page()` |
| `await page.goto(url, timeout=30_000)` | `await page.goto(url, wait_until="load", timeout_s=30)` |
| `response.status` | `response["status"]` |
| `await page.evaluate(script)` | `await page.evaluate(script)` (isolated world) |
| `await page.locator(css).click()` | `await session.human_click(page, css)` |
| `await page.fill(css, text)` | `await browser_session.element_action(page, "fill", css, [text])` |
| `await page.screenshot(path=...)` | `Path(...).write_bytes(await page.screenshot())` |
| `await page.close()` | `await session.close_page(page)` |
| `webskrap.human.click()`, `type_text()` | `session.human_click()`, `session.human_type()` |

`page.evaluate()` runs in an isolated world, as Patchright did by default. Use
`browser_session.evaluate(page, script)` to read the page's own JavaScript
globals.

Selectors are CSS or snapshot refs (`e12`). Playwright selector engines such
as `text=` and `role=` do not work.

## Other changes

- Windows is supported. On Windows, WebSkrap connects through a loopback
  debugging port. Linux and macOS use a pipe.
- `webskrap install` no longer runs `playwright install`. A browser that 2.x
  downloaded with Playwright is still found.
- Launch errors do not contain Playwright call logs. Error codes are the same.
- `ProxyConfig(server="http://user:pass@host")` is refused. Use
  `ProxyConfig(server="http://host", username="user", password="pass")`.
