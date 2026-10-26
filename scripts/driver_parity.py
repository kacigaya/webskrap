"""Compare WebSkrap's client and its bare CDP core on public anti-bot test pages.

The ``client`` path is what callers get: a WebSkrapClient session with its
page setup. The ``core`` path is a bare :func:`webskrap.cdp.launch` with the
same flags. A gap between them points at the client's page setup.

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
from typing import Any
from uuid import uuid4

from webskrap import SessionConfig, WebSkrapClient, browser_session, cdp_dom
from webskrap.browser_session import sandbox_enabled
from webskrap.cdp import CDPPage, find_executable, launch
from webskrap.challenges import _CHALLENGE_SCRIPT
from webskrap.display import VirtualDisplay
from webskrap.profiles import get_profile

REPORT = Path(".webskrap/reports/driver-parity.json")

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
# "datadome". The iframe's own document can (see _DATADOME_KIND); when it is
# readable, "datadome" becomes "captcha" or "blocked". Nothing here solves a
# captcha.
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

# Runs inside the cross-site /captcha/ iframe. DataDome marks its response page
# with dd-response-page--captcha (the slider) or dd-response-page--hard-block.
_DATADOME_KIND = """(() => {
  const marked = document.querySelector('[class*="dd-response-page--"]');
  const kind = marked && [...marked.classList].find((c) => c.startsWith('dd-response-page--'));
  return kind ? kind.slice('dd-response-page--'.length) : null;
})()"""
_DATADOME_KINDS = {"captcha": "captcha", "hard-block": "blocked"}

# Verdicts that can still change while the page settles: a DataDome device
# check can turn into the real page, so these end a run only after SETTLE_S.
_SETTLING = {"datadome", "captcha", "blocked"}
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


@dataclass
class Page:
    """What a run needs from either path."""

    evaluate: Evaluate
    screenshot: Screenshot
    #: Returns DataDome's response page kind from its iframe, or None when the
    #: path cannot read that iframe.
    datadome_kind: Callable[[], Awaitable[str | None]] | None = None


def _config(mode: str) -> SessionConfig:
    return SessionConfig(
        channel="chromium",
        headless=mode != "headed",
        virtual_display=mode == "virtual-display",
        chromium_sandbox=sandbox_enabled(),
    )


def _page(page: CDPPage) -> Page:
    async def screenshot(path: Path) -> None:
        shot = await page.send("Page.captureScreenshot", {"format": "png"})
        path.write_bytes(base64.b64decode(shot["data"]))

    # Isolated worlds: the page cannot see the checks, and they need no globals.
    async def datadome_kind() -> str | None:
        for frame in await cdp_dom.frames(page):
            if "captcha-delivery.com/" in frame.url:
                return await frame.page.evaluate(_DATADOME_KIND)
        return None

    return Page(page.evaluate, screenshot, datadome_kind)


@asynccontextmanager
async def _client(url: str, mode: str) -> AsyncIterator[Page]:
    async with WebSkrapClient() as client:
        session = await client.session("parity", config=_config(mode))
        page = await session.new_page()
        await page.goto(url, wait_until="commit", timeout_s=60)
        yield _page(page)


@asynccontextmanager
async def _core(url: str, mode: str) -> AsyncIterator[Page]:
    config = _config(mode)
    profile = get_profile(None)
    env = config.browser_env(profile)
    display = None
    if config.uses_virtual_display():
        display = await VirtualDisplay.start(1920, 1080)
        env = {**(env or os.environ), **display.env}
    browser = await launch(
        find_executable(config.channel),
        args=config.browser_args(profile),
        headless=config.launch_headless,
        env=env,
    )
    try:
        page = await browser.new_page()
        await page.enable_events()
        await page.goto(url, wait_until="commit", timeout_s=60)
        yield _page(page)
    finally:
        await browser.close()
        if display is not None:
            await display.stop()


DRIVERS = {"client": _client, "core": _core}


def _via_session() -> Callable[[str, str], AbstractAsyncContextManager[Page]]:
    """Run a target through a persistent session, as `webskrap browser` does.

    Every call attaches anew, like each CLI command or MCP tool call. Sessions
    run headless or headed; a virtual display is not offered.
    """

    @asynccontextmanager
    async def run(url: str, mode: str) -> AsyncIterator[Page]:
        if mode == "virtual-display":
            raise ValueError("sessions run headless or headed, not on a virtual display")
        # A fresh name per run: cleanup deletes the session's data, so it must
        # never reuse a session the operator opened.
        name = f"parity-{uuid4().hex[:12]}"
        await browser_session.open_session(name, headless=mode != "headed")
        try:

            async def act(action: Callable[[Any], Awaitable[Any]]) -> Any:
                return await browser_session.run_page_action(name, action, timeout_ms=60_000)

            await act(lambda page: browser_session.goto(page, url, "commit"))

            async def evaluate(script: str) -> Any:
                return await act(lambda page: browser_session.evaluate(page, script))

            async def screenshot(path: Path) -> None:
                await act(lambda page: browser_session.screenshot(page, path, full_page=False))

            yield Page(evaluate, screenshot)
        finally:
            browser_session.close_session(name, delete_data=True)

    return run


SESSION_DRIVERS = {"session": _via_session()}


async def run_once(
    driver: str,
    mode: str,
    target: str,
    wait_s: float,
    shot: Path | None = None,
    via: str = "launch",
) -> dict[str, Any]:
    url, check = TARGETS[target]
    started = time.monotonic()
    verdict: dict[str, Any] = {"state": "pending", "detail": ""}
    # A pass after a challenge means the challenge cleared; a pass without one
    # means the target never challenged this run.
    challenged = False
    try:
        drivers = SESSION_DRIVERS if via == "session" else DRIVERS
        async with drivers[driver](url, mode) as page:
            while time.monotonic() - started < wait_s:
                # A navigation can destroy the context mid-check; poll again.
                with contextlib.suppress(Exception):
                    verdict = await page.evaluate(check)
                    if verdict.get("state") == "datadome" and page.datadome_kind is not None:
                        kind = _DATADOME_KINDS.get(await page.datadome_kind() or "")
                        if kind is not None:
                            verdict = {"state": kind, "detail": f"DataDome {kind}"}
                state = verdict.get("state")
                challenged = challenged or state in ("challenge", "datadome", "captcha")
                if state == "passed":
                    break
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
        **verdict,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])
    parser.add_argument("--runs", type=int, default=3, help="Runs per path, mode and target.")
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
        "--via",
        choices=("launch", "session"),
        default="launch",
        help="launch a browser per run, or go through a persistent session.",
    )
    args = parser.parse_args()
    if args.via == "session" and "virtual-display" in (args.mode or []):
        parser.error("--via session runs headless or headed, not on a virtual display")
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
                # Alternate the order so neither path always runs first on
                # an IP that the target may start to distrust.
                paths = list(SESSION_DRIVERS if args.via == "session" else DRIVERS)
                order = paths if run % 2 == 0 else paths[::-1]
                for driver in order:
                    shot = (
                        args.screenshots / f"{target}-{mode}-{driver}-{run + 1}.png"
                        if args.screenshots
                        else None
                    )
                    result = await run_once(driver, mode, target, args.wait, shot, via=args.via)
                    results.append(result)
                    cleared = result["challenged"] and result["state"] == "passed"
                    print(
                        f"{target:13} {mode:15} {driver:10} {result['state']:9} "
                        f"{result['seconds']:5}s {result['detail']}"
                        f"{' (after challenge)' if cleared else ''}",
                        flush=True,
                    )

    print("\nPassed runs per target and mode (DataDome captcha and other pages counted apart):")
    for target in targets:
        for mode in modes:
            parts = []
            for driver in SESSION_DRIVERS if args.via == "session" else DRIVERS:
                states = [
                    r["state"]
                    for r in results
                    if (r["driver"], r["mode"], r["target"]) == (driver, mode, target)
                ]
                extra = "".join(
                    f" +{states.count(state)} {state}"
                    for state in ("captcha", "datadome")
                    if states.count(state)
                )
                parts.append(f"{driver} {states.count('passed')}/{args.runs}{extra}")
            line = "  ".join(parts)
            print(f"  {target:13} {mode:15} {line}")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
