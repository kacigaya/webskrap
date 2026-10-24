"""Compare Patchright and the CDP driver on public anti-bot test pages.

Each run starts a fresh browser and profile, loads one target, and polls a
fixed check for up to ``--wait`` seconds. The check returns ``passed``,
``challenge`` or ``pending``; a run ends at the first ``passed``. Only page
titles and verdicts are kept, never page text. The targets are public pages
built for bot and scraping tests.

Run:  python scripts/driver_parity.py --runs 3
      python scripts/driver_parity.py --mode virtual-display --target turnstile
      python scripts/driver_parity.py --datadome-url URL --target datadome --screenshots DIR
If Chromium cannot use its OS sandbox on this host:
      WEBSKRAP_CHROMIUM_SANDBOX=0 python scripts/driver_parity.py
Results are also written to .webskrap/reports/driver-parity.json.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from random import SystemRandom
from typing import Any
from uuid import uuid4

from webskrap import SessionConfig, WebSkrapClient, browser_session
from webskrap.browser_session import sandbox_enabled
from webskrap.cdp import find_executable, launch
from webskrap.cdp_fetch import launch_args
from webskrap.challenges import _CHALLENGE_SCRIPT
from webskrap.display import VirtualDisplay
from webskrap.profiles import get_profile

REPORT = Path(".webskrap/reports/driver-parity.json")
# Cursor jitter only; system entropy keeps the scanner quiet, as in human.py.
_rng = SystemRandom()

# A page passes when no known challenge is on it and its title is not a
# challenge or block title.
_GENERIC_CHECK = f"""(() => {{
  const provider = ({_CHALLENGE_SCRIPT})();
  const title = document.title.trim();
  if (provider) return {{ state: 'challenge', detail: provider }};
  if (/just a moment|attention required|access denied|blocked/i.test(title))
    return {{ state: 'challenge', detail: title.slice(0, 60) }};
  if (document.readyState !== 'complete' || !title) return {{ state: 'pending', detail: title }};
  return {{ state: 'passed', detail: title.slice(0, 60) }};
}})()"""

_TURNSTILE_CHECK = """(() => {
  const input = document.querySelector('input[name="cf-turnstile-response"]');
  if (!input || !input.value) return { state: 'pending', detail: 'no token' };
  if (input.value.includes('DUMMY')) return { state: 'challenge', detail: 'dummy token' };
  return { state: 'passed', detail: 'token' };
})()"""

_RECAPTCHA_CHECK = """(() => {
  const match = document.body.innerText.match(/"score":\\s*(\\d+\\.\\d+)/);
  if (!match) return { state: 'pending', detail: 'no score' };
  const score = parseFloat(match[1]);
  return { state: score >= 0.5 ? 'passed' : 'challenge', detail: 'score ' + score };
})()"""

# DataDome serves its slider captcha and its "access restricted" block from
# the same /captcha/ iframe, and both can carry t=fe, so the page cannot tell
# them apart: an explicit t=bv is reported as blocked, anything else as
# "datadome" for the screenshot to settle. Nothing here solves a captcha.
_DATADOME_CHECK = """(() => {
  for (const frame of document.querySelectorAll('iframe[src]')) {
    let url;
    try { url = new URL(frame.src); } catch { continue; }
    if (!url.hostname.endsWith('captcha-delivery.com')) continue;
    if (url.searchParams.get('t') === 'bv') return { state: 'blocked', detail: 'DataDome block' };
    return { state: 'datadome', detail: 'DataDome page: check the screenshot' };
  }
  if (document.readyState !== 'complete' || !document.title.trim())
    return { state: 'pending', detail: '' };
  return { state: 'passed', detail: document.title.trim().slice(0, 60) };
})()"""

# Verdicts that can still change while the page settles: a DataDome device
# check can turn into the real page, so these end a run only after SETTLE_S.
_SETTLING = {"datadome", "blocked"}
SETTLE_S = 8.0

TARGETS: dict[str, tuple[str, str]] = {
    "cf-challenge": ("https://www.scrapingcourse.com/cloudflare-challenge", _GENERIC_CHECK),
    "cf-antibot": ("https://www.scrapingcourse.com/antibot-challenge", _GENERIC_CHECK),
    "turnstile": ("https://peet.ws/turnstile-test/non-interactive.html", _TURNSTILE_CHECK),
    "recaptcha-v3": (
        "https://recaptcha-demo.appspot.com/recaptcha-v3-request-scores.php",
        _RECAPTCHA_CHECK,
    ),
}
# Not included: nowsecure.nl and antoinevastel.com/bots/datadome no longer
# challenge even curl, so a pass there says nothing.

Evaluate = Callable[[str], Awaitable[Any]]
#: Writes a PNG of the page's viewport to the given path.
Screenshot = Callable[[Path], Awaitable[None]]
#: Sends one mouse event: ("move" | "down" | "up", x, y) in viewport CSS pixels.
Mouse = Callable[[str, float, float], Awaitable[None]]


@dataclass
class Page:
    """What a run needs from either driver."""

    evaluate: Evaluate
    screenshot: Screenshot
    mouse: Mouse


# Finds the Turnstile widget: the parent of its response input, or else the
# first widget-high box. The host can span the content width while the 300 px
# iframe sits at its left edge, so only the height is checked. Returns the
# host's viewport rectangle.
_TURNSTILE_BOX = """(() => {
  const fits = (r) => r.width >= 250 && r.height >= 55 && r.height <= 80;
  const input = document.querySelector('input[name="cf-turnstile-response"]');
  const candidates = [input && input.parentElement, ...document.querySelectorAll('div')];
  for (const element of candidates) {
    if (!element) continue;
    const r = element.getBoundingClientRect();
    if (fits(r)) return { x: r.left, y: r.top, w: r.width, h: r.height };
  }
  return null;
})()"""


async def human_click(mouse: Mouse, x: float, y: float) -> None:
    """Move along an eased, jittered curve to (x, y), then press and release.

    Both drivers get the same path through their own CDP input events, so the
    comparison measures the driver, not the cursor model.
    """
    start_x, start_y = x + _rng.uniform(250, 450), y + _rng.uniform(150, 300)
    control_x, control_y = (start_x + x) / 2 + _rng.uniform(-80, 80), start_y - 60
    steps = _rng.randint(22, 32)
    for step in range(1, steps + 1):
        t = step / steps
        t = t * t * (3 - 2 * t)  # ease in and out
        px = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * control_x + t * t * x
        py = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * control_y + t * t * y
        await mouse("move", px + _rng.uniform(-0.8, 0.8), py + _rng.uniform(-0.8, 0.8))
        await asyncio.sleep(_rng.uniform(0.008, 0.022))
    await asyncio.sleep(_rng.uniform(0.12, 0.3))
    await mouse("down", x, y)
    await asyncio.sleep(_rng.uniform(0.06, 0.12))
    await mouse("up", x, y)


def _config(mode: str) -> SessionConfig:
    return SessionConfig(
        channel="chromium",
        headless=mode != "headed",
        virtual_display=mode == "virtual-display",
        chromium_sandbox=sandbox_enabled(),
    )


@asynccontextmanager
async def _patchright(url: str, mode: str) -> AsyncIterator[Page]:
    config = _config(mode).model_copy(update={"driver": "patchright"})
    async with WebSkrapClient() as client:
        session = await client.session("parity", config=config)
        page = await session.context.new_page()
        await page.goto(url, wait_until="commit", timeout=60_000)

        async def screenshot(path: Path) -> None:
            await page.screenshot(path=str(path))

        async def mouse(kind: str, x: float, y: float) -> None:
            if kind == "move":
                await page.mouse.move(x, y)
            elif kind == "down":
                await page.mouse.down()
            else:
                await page.mouse.up()

        yield Page(page.evaluate, screenshot, mouse)


@asynccontextmanager
async def _cdp(url: str, mode: str) -> AsyncIterator[Page]:
    config = _config(mode)
    args, env = launch_args(config, get_profile(None))
    display = None
    if config.uses_virtual_display():
        display = await VirtualDisplay.start(1920, 1080)
        env = {**os.environ, **display.env}
    browser = await launch(
        find_executable(config.channel),
        args=args,
        headless=config.headless and not config.virtual_display,
        env=env or None,
    )
    try:
        page = await browser.new_page()
        await page.send("Page.enable")
        await page.send("Page.navigate", {"url": url}, timeout_s=60)

        async def screenshot(path: Path) -> None:
            shot = await page.send("Page.captureScreenshot", {"format": "png"})
            path.write_bytes(base64.b64decode(shot["data"]))

        async def mouse(kind: str, x: float, y: float) -> None:
            # Same events Playwright's mouse sends: a held left button
            # reports buttons=1 until it is released.
            event = {"move": "mouseMoved", "down": "mousePressed", "up": "mouseReleased"}[kind]
            pressed = kind == "down"
            await page.send(
                "Input.dispatchMouseEvent",
                {
                    "type": event,
                    "x": x,
                    "y": y,
                    "button": "none" if kind == "move" else "left",
                    "buttons": 1 if pressed else 0,
                    "clickCount": 0 if kind == "move" else 1,
                },
            )

        yield Page(page.evaluate, screenshot, mouse)
    finally:
        await browser.close()
        if display is not None:
            await display.stop()


DRIVERS = {"patchright": _patchright, "cdp": _cdp}


def _via_session(driver: str) -> Callable[[str, str], AbstractAsyncContextManager[Page]]:
    """Run a target through a persistent session, as `webskrap browser` does.

    Every call attaches anew, like each CLI command or MCP tool call. Sessions
    run headless or headed; a virtual display and clicks are not offered.
    """

    @asynccontextmanager
    async def run(url: str, mode: str) -> AsyncIterator[Page]:
        if mode == "virtual-display":
            raise ValueError("sessions run headless or headed, not on a virtual display")
        # A fresh name per run: cleanup deletes the session's data, so it must
        # never reuse a session the operator opened.
        name = f"parity-{driver}-{uuid4().hex[:12]}"
        await browser_session.open_session(
            name, headless=mode != "headed", driver="cdp" if driver == "cdp" else "patchright"
        )
        try:

            async def act(action: Callable[[Any], Awaitable[Any]]) -> Any:
                return await browser_session.run_page_action(name, action, timeout_ms=60_000)

            await act(lambda page: browser_session.goto(page, url, "commit"))

            async def evaluate(script: str) -> Any:
                return await act(lambda page: browser_session.evaluate(page, script))

            async def screenshot(path: Path) -> None:
                await act(lambda page: browser_session.screenshot(page, path, full_page=False))

            async def mouse(_kind: str, _x: float, _y: float) -> None:
                raise NotImplementedError("--click is not offered with --via session")

            yield Page(evaluate, screenshot, mouse)
        finally:
            browser_session.close_session(name, delete_data=True)

    return run


SESSION_DRIVERS = {driver: _via_session(driver) for driver in DRIVERS}

# The widget box renders before the checkbox inside its iframe does.
WIDGET_SETTLE_S = 3.0


async def _click_turnstile(page: Page, shot: Path | None) -> bool:
    """Click the Turnstile checkbox once it shows; False while it is absent."""
    box = None
    with contextlib.suppress(Exception):
        box = await page.evaluate(_TURNSTILE_BOX)
    if not box:
        await asyncio.sleep(1)
        return False
    await asyncio.sleep(WIDGET_SETTLE_S)
    if shot is not None:
        with contextlib.suppress(Exception):
            shot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(shot.with_name(f"{shot.stem}-before-click.png"))
    # The checkbox sits near the widget's left edge, centered vertically.
    x = box["x"] + 22 + _rng.uniform(-3, 3)
    y = box["y"] + box["h"] / 2 + _rng.uniform(-3, 3)
    await human_click(page.mouse, x, y)
    return True


async def run_once(
    driver: str,
    mode: str,
    target: str,
    wait_s: float,
    shot: Path | None = None,
    click: bool = False,
    via: str = "launch",
) -> dict[str, Any]:
    url, check = TARGETS[target]
    started = time.monotonic()
    verdict: dict[str, Any] = {"state": "pending", "detail": ""}
    # A pass after a challenge means the challenge cleared; a pass without one
    # means the target never challenged this run.
    challenged = False
    clicked = False
    try:
        drivers = SESSION_DRIVERS if via == "session" else DRIVERS
        async with drivers[driver](url, mode) as page:
            while time.monotonic() - started < wait_s:
                # A navigation can destroy the context mid-check; poll again.
                with contextlib.suppress(Exception):
                    verdict = await page.evaluate(check)
                state = verdict.get("state")
                challenged = challenged or state in ("challenge", "datadome")
                if state == "passed":
                    break
                if click and not clicked and state == "challenge":
                    clicked = await _click_turnstile(page, shot)
                    continue
                if state in _SETTLING and time.monotonic() - started >= SETTLE_S:
                    break
                await asyncio.sleep(1)
            if shot is not None:
                # The last state is what the verdict describes; a failed
                # capture must not turn a verdict into an error.
                with contextlib.suppress(Exception):
                    shot.parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(shot)
    except Exception as exc:  # noqa: BLE001 - one failed run must not stop the batch
        verdict = {"state": "error", "detail": f"{type(exc).__name__}: {str(exc)[:80]}"}
    return {
        "driver": driver,
        "mode": mode,
        "target": target,
        "seconds": round(time.monotonic() - started, 1),
        "challenged": challenged,
        "clicked": clicked,
        **verdict,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=3, help="Runs per driver, mode and target.")
    parser.add_argument("--wait", type=float, default=30, help="Seconds to poll each run.")
    parser.add_argument(
        "--mode", action="append", choices=("headless", "virtual-display", "headed")
    )
    parser.add_argument(
        "--datadome-url",
        help="Also run a DataDome website you may test, as target 'datadome'.",
    )
    parser.add_argument("--target", action="append")
    parser.add_argument(
        "--screenshots",
        type=Path,
        help="Write a PNG of each run's last state to this directory.",
    )
    parser.add_argument(
        "--click",
        action="store_true",
        help="Click a Cloudflare Turnstile checkbox when a challenge shows it.",
    )
    parser.add_argument(
        "--via",
        choices=("launch", "session"),
        default="launch",
        help="launch a browser per run, or go through a persistent session.",
    )
    args = parser.parse_args()
    if args.via == "session" and (args.click or "virtual-display" in (args.mode or [])):
        parser.error("--via session runs headless or headed, without --click")
    modes = args.mode or (
        ["headless"] if args.via == "session" else ["headless", "virtual-display"]
    )
    if args.datadome_url:
        # Taken at run time so no third-party site is named in the repository.
        TARGETS["datadome"] = (args.datadome_url, _DATADOME_CHECK)
    unknown = sorted(set(args.target or []) - set(TARGETS))
    if unknown:
        parser.error(f"unknown target(s): {', '.join(unknown)}")
    targets = args.target or list(TARGETS)

    results = []
    for target in targets:
        for mode in modes:
            for run in range(args.runs):
                # Alternate the order so neither driver always runs first on
                # an IP that the target may start to distrust.
                order = ("patchright", "cdp") if run % 2 == 0 else ("cdp", "patchright")
                for driver in order:
                    shot = (
                        args.screenshots / f"{target}-{mode}-{driver}-{run + 1}.png"
                        if args.screenshots
                        else None
                    )
                    result = await run_once(
                        driver, mode, target, args.wait, shot, click=args.click, via=args.via
                    )
                    results.append(result)
                    cleared = result["challenged"] and result["state"] == "passed"
                    print(
                        f"{target:13} {mode:15} {driver:10} {result['state']:9} "
                        f"{result['seconds']:5}s {result['detail']}"
                        f"{' (after challenge)' if cleared else ''}"
                        f"{' [clicked]' if result['clicked'] else ''}",
                        flush=True,
                    )

    print("\nPassed runs per target and mode (DataDome pages counted separately):")
    for target in targets:
        for mode in modes:
            parts = []
            for driver in DRIVERS:
                states = [
                    r["state"]
                    for r in results
                    if (r["driver"], r["mode"], r["target"]) == (driver, mode, target)
                ]
                datadome = states.count("datadome")
                extra = f" +{datadome} datadome" if datadome else ""
                parts.append(f"{driver} {states.count('passed')}/{args.runs}{extra}")
            line = "  ".join(parts)
            print(f"  {target:13} {mode:15} {line}")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
