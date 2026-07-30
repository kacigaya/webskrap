# Humanizer

WebSkrap's humanizer is the `human_click` session helper. It waits for a visible
target, scrolls it into view, moves the mouse along a curved eased path, and then
clicks near the target center instead of calling Playwright's direct
`page.click`.

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

## Direct click fallback

Pass `human=False` to keep the same call shape while delegating to Playwright's
click implementation.

```python
await session.human_click(page, "button[type='submit']", human=False, timeout=5_000)
```

## Click options

`human_click` accepts normal Playwright click options including `button`,
`click_count`, `delay`, `modifiers`, `position`, `strict`, `timeout`, and
`trial`.

```python
await session.human_click(
    page,
    "button[type='submit']",
    strict=True,
    timeout=10_000,
    modifiers=["Shift"],
)
```
