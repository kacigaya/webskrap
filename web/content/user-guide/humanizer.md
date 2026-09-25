# Humanizer

WebSkrap's humanizer drives the real mouse and keyboard with human timing
instead of Playwright's direct input. `human_click` waits for a visible target,
scrolls it into view with the mouse wheel, moves the mouse along a curved eased
path, and clicks near the target center with a human button hold;
`human_type` clicks into a field the same way and types one key at a time.

```python
async with WebSkrapClient() as client:
    session = await client.session("default")
    page = await session.context.new_page()
    await page.goto("https://example.com", wait_until="domcontentloaded")
    await session.human_click(page, "button[type='submit']")
```

Use it when a normal page interaction should look closer to a manual browser
click. It is useful for flows with hover-sensitive controls, scroll-dependent
layouts, or simple behavioral checks that treat instant coordinate jumps as
synthetic.

The cursor follows a cubic Bezier curve with randomized control points and
eased spacing, so it accelerates and slows near the target instead of tracing a
straight, evenly spaced line. The algorithm is adapted from
[HumanCursor](https://github.com/riflosnake/HumanCursor) and reimplemented for
Playwright's async mouse without adding a dependency.

What a page's scripts see, compared with Playwright's own input:

| Signal | Playwright | Humanizer |
|---|---|---|
| Scrolling to an off-screen target | a jump with no `wheel` events | `wheel` events in notch-sized steps with short pauses |
| Cursor before the click | teleports to the target | a curved path of `mousemove` events |
| Button hold (`mousedown` to `mouseup`) | about 2 ms | 60-140 ms, unless you pass `delay` |
| Typing | `fill`: no key events; `press_sequentially`: a constant rate | each key held 30-90 ms, 40-160 ms apart, with an occasional longer pause |

Before any pointer event, the humanizer checks with `elementFromPoint` that
nothing covers the exact click point, in the element's own frame and shadow
root, and raises a `usage` error if something does. It does not use
Playwright's `click(trial=True)` for that: a trial click scrolls the page and
sends a `mousemove` and a `click` event the page can see.

If the mouse wheel cannot move the target into view, for example inside an
inner scroll container the cursor is not over, Playwright's scroll finishes the
job.

## Typing

```python
await session.human_type(page, "input[name='q']", "running shoes")
```

The text is appended to what the field holds, as with Playwright's
`press_sequentially`. `timeout` is honored.

## Where it applies automatically

- Cookie consent dismissal (`decline_cookies`) clicks the reject control with
  the humanized click, including notices inside CMP iframes. The banner is
  usually the first thing clicked on a page, and the page's bot script sees
  that click too.
- Persistent sessions: `webskrap browser click` / `dblclick` and MCP
  `browser_interact` with `click` or `dblclick` use the humanized click, and
  `type` uses human typing. `fill` still sets the value at once, for speed.

Humanized input is slower by design: a click takes roughly half a second to a
second, and typing about a tenth of a second per character.

## Direct click fallback

Pass `human=False` to keep the same call shape while delegating to Playwright's
click implementation.

```python
await session.human_click(page, "button[type='submit']", human=False, timeout=5_000)
```

## Click options

`human_click` accepts normal Playwright click options including `button`,
`click_count`, `delay`, `modifiers`, `position`, `strict`, `timeout`, and
`trial`. `delay` sets the button hold in milliseconds; `trial` runs the checks,
including the coverage check, without moving the mouse.

```python
await session.human_click(
    page,
    "button[type='submit']",
    strict=True,
    timeout=10_000,
    modifiers=["Shift"],
)
```
