"""Headless live bot-detection tests for WebSkrap's patchright driver.

These are opt-in live tests:

    WEBSKRAP_LIVE=1 pytest tests/test_bot_detection_headless.py

They exercise the same public demos as ``test_bot_detection.py`` but run with
``headless=True``. Explicit headless-only signals are tolerated; webdriver, CDP,
Playwright, and stealth/tampering signals are not. The suite uses its own
persistent Chrome profile so results can be compared with the headed live suite.

Set ``WEBSKRAP_LIVE_VIRTUAL_DISPLAY=1`` to run the suite with
``virtual_display=True`` (Linux with Xvfb) instead of masking the headless user
agent, so Chromium runs headed on a private Xvfb screen.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from live_stealth_helpers import live_proxy, wait_for_recaptcha_score_or_skip

from webskrap import SessionConfig, Viewport, WebSkrapClient
from webskrap.browser_session import sandbox_enabled

pytestmark = [pytest.mark.browser, pytest.mark.live]

LIVE_HEADLESS_PROFILE_DIR = Path(
    os.environ.get("WEBSKRAP_LIVE_HEADLESS_PROFILE_DIR", ".webskrap/live-headless-profile")
)


STEALTH_HEADLESS = SessionConfig(
    driver="patchright",
    channel=os.environ.get("WEBSKRAP_BROWSER_CHANNEL", "chrome"),
    headless=True,
    chromium_sandbox=sandbox_enabled(None),
    user_data_dir=LIVE_HEADLESS_PROFILE_DIR,
    headless_screen=Viewport(width=1366, height=768),
    mask_headless_user_agent=True,
    virtual_display=os.environ.get("WEBSKRAP_LIVE_VIRTUAL_DISPLAY") == "1",
    proxy=live_proxy(),
    webrtc_ip_handling_policy="disable_non_proxied_udp",
)

_HEADLESS_LABELS = {
    "HEADCHR_UA",
    "HEADCHR_CHROME_OBJ",
    "CHR_MEMORY",
    "TRANSPARENT_PIXEL",
    "SEQUENTIA",
    "User Agent\n(Old)",
}
_INCOL_HEADLESS_LABELS = {
    "HEADCHR_UA",
    "HEADCHR_CHROME_OBJ",
    "HEADCHR_PERMISSIONS",
    "CHR_MEMORY",
    "TRANSPARENT_PIXEL",
    "SEQUENTIA",
    "VIDEO_CODECS",
    "userAgent",
}


@pytest.fixture(autouse=True)
def _require_live() -> None:
    if not os.environ.get("WEBSKRAP_LIVE"):
        pytest.skip("set WEBSKRAP_LIVE=1 to run live bot-detection tests")


@asynccontextmanager
async def stealth_headless_page():
    client = WebSkrapClient(default_config=STEALTH_HEADLESS)
    try:
        await client.start()
        session = await client.session(
            "bot-detect-headless",
            config=STEALTH_HEADLESS,
            profile="desktop-chrome",
        )
        page = await session.context.new_page()
    except Exception as exc:  # noqa: BLE001 - environment guard, re-raised as skip
        await client.close()
        pytest.skip(f"Playwright browser unavailable: {exc}")
    try:
        yield page
    finally:
        await client.close()


def _unexpected(failed: list[str], allowed: set[str]) -> list[str]:
    return [name for name in failed if name not in allowed]


async def test_recaptcha_v3_headless() -> None:
    async with stealth_headless_page() as page:
        await page.goto(
            "https://recaptcha-demo.appspot.com/recaptcha-v3-request-scores.php",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await wait_for_recaptcha_score_or_skip(page)
        score = await page.evaluate(
            """() => {
                const m = document.body.innerText.match(/"score":\\s*(\\d+\\.\\d+)/);
                return m ? parseFloat(m[1]) : null;
            }"""
        )
    assert score is not None, "could not extract reCAPTCHA v3 score"


async def test_cloudflare_turnstile_headless_loads() -> None:
    async with stealth_headless_page() as page:
        await page.goto(
            "https://2captcha.com/demo/cloudflare-turnstile",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await page.wait_for_timeout(10_000)
        result = await page.evaluate(
            """() => {
                const response = document.querySelector(
                    'input[name="cf-turnstile-response"]'
                );
                const iframe = document.querySelector('iframe[src*="turnstile"]');
                return {hasSurface: Boolean(response || iframe)};
            }"""
        )
    assert result["hasSurface"], "Turnstile surface did not render"


async def test_browserscan_bot_detection_headless() -> None:
    async with stealth_headless_page() as page:
        await page.goto(
            "https://www.browserscan.net/bot-detection",
            wait_until="networkidle",
            timeout=60_000,
        )
        await page.wait_for_timeout(5_000)
        result = await page.evaluate(
            """() => {
                const text = document.body.innerText;
                const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
                const indexOf = (label) => lines.findIndex(line => line === label);
                const statusAfter = (label) => {
                    const index = indexOf(label);
                    if (index === -1) return null;
                    const tail = lines.slice(index + 1, index + 5).join(' ');
                    if (/Abnormal/i.test(tail)) return 'Abnormal';
                    if (/Normal/i.test(tail)) return 'Normal';
                    return null;
                };
                return {
                    text,
                    webdriver: statusAfter('Webdriver'),
                    cdp: statusAfter('CDP'),
                    navigator: statusAfter('Navigator'),
                    userAgent: statusAfter('User-Agent'),
                };
            }"""
        )
    assert result["webdriver"] != "Abnormal", (
        f"BrowserScan webdriver abnormal: {result['text'][:500]}"
    )
    assert result["cdp"] != "Abnormal", f"BrowserScan CDP abnormal: {result['text'][:500]}"
    assert result["navigator"] != "Abnormal", (
        f"BrowserScan navigator abnormal: {result['text'][:500]}"
    )


async def test_fingerprintjs_web_scraping_demo_headless() -> None:
    async with stealth_headless_page() as page:
        await page.goto(
            "https://demo.fingerprint.com/web-scraping",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await page.wait_for_timeout(8_000)
        await page.get_by_role("button", name="Search").click(timeout=8_000)
        await page.wait_for_timeout(6_000)
        result = await page.evaluate(
            """() => {
                const text = document.body.innerText;
                const lower = text.toLowerCase();
                const blocked =
                    lower.includes('access denied') ||
                    lower.includes('request was blocked') ||
                    lower.includes('bot visit detected');
                const searched = lower.includes('search') || lower.includes('flights');
                const flights = text.includes('$') || text.includes('Buy');
                return {blocked, searched, flights};
            }"""
        )
    assert result["searched"], "FingerprintJS demo did not load search UI"
    assert result["flights"] or result["blocked"], "FingerprintJS returned no result"


async def test_device_and_browser_info_behavioral_headless() -> None:
    async with stealth_headless_page() as page:
        await page.goto(
            "https://deviceandbrowserinfo.com/are_you_a_bot",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await page.wait_for_timeout(8_000)
        result = await page.evaluate(
            """() => {
                const text = document.body.innerText;
                const signals = {};
                ['isBot', 'hasWebdriverTrue', 'isHeadlessChrome',
                 'isAutomatedWithCDP', 'isPlaywright'].forEach(p => {
                    const m = text.match(
                        new RegExp('"' + p + '":\\\\s*(true|false)')
                    );
                    if (m) signals[p] = m[1] === 'true';
                });
                return {signals};
            }"""
        )
    assert result["signals"].get("hasWebdriverTrue") is not True, result["signals"]
    assert result["signals"].get("isAutomatedWithCDP") is not True, result["signals"]
    assert result["signals"].get("isPlaywright") is not True, result["signals"]


async def test_bot_sannysoft_headless() -> None:
    async with stealth_headless_page() as page:
        await page.goto(
            "https://bot.sannysoft.com",
            wait_until="networkidle",
            timeout=60_000,
        )
        await page.wait_for_timeout(4_000)
        failed = await page.evaluate(
            """() => {
                const failed = [];
                document.querySelectorAll('table tr').forEach(r => {
                    const cells = r.querySelectorAll('td');
                    if (cells.length >= 2 && (cells[1].className || '').includes('failed')) {
                        failed.push(cells[0].innerText.trim());
                    }
                });
                return failed;
            }"""
        )
    unexpected = _unexpected(failed, _HEADLESS_LABELS)
    assert unexpected == [], f"Sannysoft unexpected failures: {', '.join(unexpected)}"


async def test_bot_incolumitas_headless() -> None:
    known_acceptable = {"WEBDRIVER", "connectionRTT"} | _INCOL_HEADLESS_LABELS
    async with stealth_headless_page() as page:
        await page.goto(
            "https://bot.incolumitas.com",
            wait_until="networkidle",
            timeout=60_000,
        )
        await page.wait_for_timeout(13_000)
        failed = await page.evaluate(
            """() => {
                const text = document.body.innerText;
                const fails = text.match(/"(\\w+)":\\s*"FAIL"/g) || [];
                return fails.map(m => m.match(/"(\\w+)"/)[1]);
            }"""
        )
    unexpected = _unexpected(failed, known_acceptable)
    assert unexpected == [], f"Incolumitas unexpected failures: {', '.join(unexpected)}"


async def test_are_you_headless_headless() -> None:
    async with stealth_headless_page() as page:
        await page.goto(
            "https://arh.antoinevastel.com/bots/areyouheadless",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await page.wait_for_timeout(5_000)
        text = await page.evaluate("() => document.body.innerText")
    assert "You are not Chrome headless" in text, "headless browser exposed Chrome headless"


_CREEPJS_EVAL = """() => {
    const blockFor = (label) => {
        let block = "";
        document.querySelectorAll('*').forEach(el => {
            if (el.children.length === 0 && (el.textContent || '').trim() === label) {
                const p = el.parentElement;
                if (p) block = (p.textContent || '').replace(/\\s+/g, ' ').trim();
            }
        });
        return block;
    };
    return {headless: blockFor('Headless'), stealth: blockFor('Stealth')};
}"""


async def test_creepjs_headless() -> None:
    async with stealth_headless_page() as page:
        await page.goto(
            "https://abrahamjuliot.github.io/creepjs/",
            wait_until="networkidle",
            timeout=60_000,
        )
        await page.wait_for_timeout(16_000)
        signals = await page.evaluate(_CREEPJS_EVAL)
    assert signals["headless"], "CreepJS Headless block did not render"
    assert signals["stealth"], "CreepJS Stealth block did not render"
    assert ": true" not in signals["stealth"], (
        f"CreepJS flagged a stealth/tampering signal: {signals['stealth']}"
    )
