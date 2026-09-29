"""Live bot-detection tests for WebSkrap's stealth features.

These drive a stealth-configured browser against public bot-detection demos and
assert that WebSkrap is not flagged. They require network access and real
third-party services, so they are opt-in:

    WEBSKRAP_LIVE=1 pytest tests/test_bot_detection.py

They are skipped by default (and in CI) because the services are slow and may
change their markup or scoring at any time. They only exercise public detection
demos meant for this purpose — no CAPTCHA solving or access-control bypass.

They pass with the patchright stealth driver (``driver="patchright"``), which is
a CDP-leak-free Playwright fork, run headed against the real Chrome channel
with a persistent Chrome profile:

- patchright hides the CDP ``Runtime.enable`` leak (``isAutomatedWithCDP``);
- ``user_data_dir`` keeps Chrome profile state stable between live runs;
- ``webrtc_ip_handling_policy`` blocks non-proxied UDP ICE candidates;
- the real Chrome channel avoids FingerprintJS's anti-detect tampering signal
  that flags patchright's bundled Chromium;
- WebSkrap avoids JavaScript fingerprint spoofing and synthetic profile
  injection, so the browser's real fingerprint shows through instead of a
  spoofed one.

Compare this strict headed suite with ``test_bot_detection_headless.py`` for the
current headless baseline. Requires Google Chrome installed on the host and the
Patchright browser download (``webskrap install``).
"""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager, suppress
from ipaddress import ip_address
from json import loads
from pathlib import Path

import pytest
from live_stealth_helpers import live_proxy, wait_for_recaptcha_score_or_skip

from webskrap import SessionConfig, WebSkrapClient
from webskrap.browser_session import sandbox_enabled

pytestmark = [pytest.mark.browser, pytest.mark.live]

LIVE_PROFILE_DIR = Path(
    os.environ.get("WEBSKRAP_LIVE_PROFILE_DIR", ".webskrap/live-stealth-profile")
)


# patchright is a CDP-leak-free Playwright fork; it is the stealth mechanism.
# Run headed against the real Chrome channel with a stable profile. Google
# Chrome must be installed on the host.
STEALTH = SessionConfig(
    driver="patchright",
    channel=os.environ.get("WEBSKRAP_BROWSER_CHANNEL", "chrome"),
    headless=False,
    chromium_sandbox=sandbox_enabled(None),
    user_data_dir=LIVE_PROFILE_DIR,
    proxy=live_proxy(),
    webrtc_ip_handling_policy="disable_non_proxied_udp",
)


@pytest.fixture(autouse=True)
def _require_live() -> None:
    if not os.environ.get("WEBSKRAP_LIVE"):
        pytest.skip("set WEBSKRAP_LIVE=1 to run live bot-detection tests")


@asynccontextmanager
async def stealth_page():
    # Yield a stealth page, skipping if Playwright/browser is unavailable.
    client = WebSkrapClient(default_config=STEALTH)
    try:
        await client.start()
        session = await client.session("bot-detect", config=STEALTH, profile="desktop-chrome")
        page = await session.context.new_page()
    except Exception as exc:  # noqa: BLE001 - environment guard, re-raised as skip
        await client.close()
        pytest.skip(f"Playwright browser unavailable: {exc}")
    try:
        yield page
    finally:
        await client.close()


def _ip_literals(text: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
        with suppress(ValueError):
            found.append(str(ip_address(match.group(0))))
    return found


def _private_candidate_ips(text: str) -> list[str]:
    lines = [
        line
        for line in text.splitlines()
        if "candidate" in line.lower() or "ip address" in line.lower()
    ]
    return [
        ip
        for ip in _ip_literals("\n".join(lines))
        if (ip_address(ip).is_private or ip_address(ip).is_loopback or ip_address(ip).is_link_local)
    ]


def _is_public_ip(value: str) -> bool:
    parsed = ip_address(value)
    return not (parsed.is_private or parsed.is_loopback or parsed.is_link_local)


async def _json_body(page, url: str) -> dict:
    response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    assert response and response.status < 400, (
        f"{url} returned {response.status if response else None}"
    )
    return loads(await page.locator("body").inner_text(timeout=10_000))


async def _dnsleaktest_rows(page) -> list[dict[str, str]]:
    return await page.evaluate(
        """() => {
            const table = document.querySelector('table');
            if (!table) return [];
            const rows = [...table.querySelectorAll('tr')].map((tr) =>
                [...tr.children].map((td) => td.innerText.trim())
            );
            return rows.slice(1).map(([ip, hostname, isp, country]) => ({
                ip,
                hostname,
                isp,
                country,
            })).filter((row) => row.ip);
        }"""
    )


async def test_recaptcha_v3() -> None:
    # reCAPTCHA v3 score must be >= 0.7 (human range).
    async with stealth_page() as page:
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
    assert score >= 0.7, f"reCAPTCHA v3 score too low: {score}"


async def test_cloudflare_turnstile_demo() -> None:
    # Verify the public demo renders the Turnstile surface. WebSkrap does not
    # solve or submit CAPTCHA challenges.
    async with stealth_page() as page:
        await page.goto(
            "https://2captcha.com/demo/cloudflare-turnstile",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await page.wait_for_function(
            """() => {
                const widget = document.querySelector('.cf-turnstile');
                const el = document.querySelector(
                    'input[name="cf-turnstile-response"]'
                );
                return Boolean(widget || el);
            }""",
            timeout=45_000,
        )
        result = await page.evaluate(
            """() => {
                const widget = document.querySelector('.cf-turnstile');
                const response = document.querySelector(
                    'input[name="cf-turnstile-response"]'
                );
                const iframe = document.querySelector('iframe[src*="turnstile"]');
                return {
                    hasSurface: Boolean(widget || response || iframe),
                };
            }"""
        )
    assert result["hasSurface"], "Turnstile surface did not render"


async def test_browserscan_bot_detection() -> None:
    # BrowserScan must report 0 abnormal checks.
    async with stealth_page() as page:
        await page.goto(
            "https://www.browserscan.net/bot-detection",
            wait_until="networkidle",
            timeout=60_000,
        )
        await page.wait_for_timeout(5_000)
        result = await page.evaluate(
            """() => {
                const text = document.body.innerText;
                return {
                    normal: (text.match(/Normal/g) || []).length,
                    abnormal: (text.match(/Abnormal/g) || []).length,
                };
            }"""
        )
    assert result["abnormal"] == 0, (
        f"BrowserScan: {result['abnormal']} abnormal, {result['normal']} normal"
    )


async def test_fingerprintjs_web_scraping_demo() -> None:
    # FingerprintJS web-scraping demo must return flight prices, not block us.
    async with stealth_page() as page:
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
                const blocked =
                    text.includes('access denied') ||
                    text.includes('request was blocked') ||
                    text.includes('bot visit detected');
                const flights = text.includes('$') || text.includes('Buy');
                return {blocked, flights};
            }"""
        )
    assert not result["blocked"], "FingerprintJS blocked us as a bot"
    assert result["flights"], "FingerprintJS: no flight data returned"


async def test_pixelscan_audit_loads_without_explicit_block() -> None:
    async with stealth_page() as page:
        await page.goto("https://pixelscan.net", wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_function(
            """() => /pixelscan|fingerprint|bot/i.test(document.body.innerText)""",
            timeout=30_000,
        )
        text = await page.evaluate("() => document.body.innerText")
    lower = text.lower()
    assert "pixelscan" in lower or "fingerprint" in lower, text[:500]
    assert "access denied" not in lower and "request was blocked" not in lower, text[:500]


async def test_browserscan_main_page_renders_fingerprint_summary() -> None:
    async with stealth_page() as page:
        await page.goto("https://www.browserscan.net", wait_until="networkidle", timeout=60_000)
        await page.wait_for_function(
            """() => /browserscan|fingerprint|browser/i.test(document.body.innerText)""",
            timeout=30_000,
        )
        text = await page.evaluate("() => document.body.innerText")
    lower = text.lower()
    assert "browserscan" in lower
    assert "access denied" not in lower and "request was blocked" not in lower, text[:500]


async def test_fingerprint_scan_page_renders_score_surface() -> None:
    async with stealth_page() as page:
        await page.goto("https://fingerprint-scan.com", wait_until="networkidle", timeout=60_000)
        await page.wait_for_function(
            """() => /fingerprint|scan|score/i.test(document.body.innerText)""",
            timeout=30_000,
        )
        text = await page.evaluate("() => document.body.innerText")
    lower = text.lower()
    assert "fingerprint" in lower
    assert "webdriver true" not in lower and "headlesschrome" not in lower, text[:500]


async def test_browserleaks_webrtc_policy_hides_private_candidates() -> None:
    async with stealth_page() as page:
        await page.goto("https://browserleaks.com/webrtc", wait_until="networkidle", timeout=60_000)
        await page.wait_for_function(
            """() => /webrtc|rtcpeerconnection|candidate/i.test(document.body.innerText)""",
            timeout=30_000,
        )
        text = await page.evaluate("() => document.body.innerText")
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in STEALTH.launch_options()["args"]
    assert _private_candidate_ips(text) == [], text[:1000]


async def test_browserleaks_client_hints_no_headless_ua() -> None:
    async with stealth_page() as page:
        await page.goto(
            "https://browserleaks.com/client-hints",
            wait_until="networkidle",
            timeout=60_000,
        )
        await page.wait_for_function(
            """() => /client hints|sec-ch-ua|user-agent/i.test(document.body.innerText)""",
            timeout=30_000,
        )
        text = await page.evaluate("() => document.body.innerText")
    assert "HeadlessChrome" not in text
    assert "Sec-CH-UA" in text or "Client Hints" in text


async def test_browserleaks_tls_reports_fingerprint_surface() -> None:
    async with stealth_page() as page:
        await page.goto("https://tls.browserleaks.com", wait_until="networkidle", timeout=60_000)
        await page.wait_for_function(
            """() => /tls|ja3|ja4|fingerprint/i.test(document.body.innerText)""",
            timeout=30_000,
        )
        text = await page.evaluate("() => document.body.innerText")
    lower = text.lower()
    assert "tls" in lower
    assert "ja3" in lower or "ja4" in lower or "fingerprint" in lower


async def test_tls_ja3_visibility_and_proxy_consistency() -> None:
    async with stealth_page() as page:
        http_ip = await _json_body(page, "https://httpbingo.org/ip")
        tls_report = await _json_body(page, "https://tls.peet.ws/api/all")

    origin = str(http_ip.get("origin", "")).split(",")[0].strip()
    tls_ip = str(tls_report.get("ip", "")).strip()
    assert origin, http_ip
    assert tls_ip, tls_report
    assert "ja3" in str(tls_report).lower() or "ja4" in str(tls_report).lower()
    if STEALTH.proxy:
        assert origin == tls_ip, f"proxy mismatch: http={origin}, tls={tls_ip}"


async def test_dns_leak_surface_renders() -> None:
    async with stealth_page() as page:
        await page.goto("https://dnsleaktest.com", wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_function(
            """() => /dns leak test/i.test(document.title) ||
                /what is a dns leak|webrtc leak test|standard test|extended test/i
                    .test(document.body.innerText)""",
            timeout=30_000,
        )
        text = await page.evaluate("() => document.body.innerText")
    assert "dns leak" in text.lower() or "webrtc leak test" in text.lower(), text[:1000]


async def test_dns_standard_test_resolvers_are_public_and_proxy_consistent() -> None:
    async with stealth_page() as page:
        await page.goto("https://dnsleaktest.com", wait_until="domcontentloaded", timeout=60_000)
        await page.get_by_role("button", name="Standard test").click(timeout=10_000)
        await page.wait_for_function(
            """() => /test complete/i.test(document.body.innerText) &&
                document.querySelectorAll('table tr').length > 1""",
            timeout=45_000,
        )
        text = await page.evaluate("() => document.body.innerText")
        rows = await _dnsleaktest_rows(page)

    public_ip_match = re.search(r"Your public IP:\s*((?:\d{1,3}\.){3}\d{1,3})", text)
    public_ip = public_ip_match.group(1) if public_ip_match else ""
    assert public_ip and _is_public_ip(public_ip), text[:1000]
    assert rows, text[:1000]
    resolver_ips = [row["ip"] for row in rows]
    assert all(_is_public_ip(ip) for ip in resolver_ips), rows

    expected_ip = os.environ.get("WEBSKRAP_LIVE_EXPECTED_PUBLIC_IP")
    expected_country = os.environ.get("WEBSKRAP_LIVE_EXPECTED_COUNTRY")
    if expected_ip:
        assert public_ip == expected_ip, f"public IP mismatch: {public_ip} != {expected_ip}"
    if expected_country:
        mismatched = [row for row in rows if expected_country.lower() not in row["country"].lower()]
        assert mismatched == [], f"DNS resolver country mismatch: {mismatched}"
    if STEALTH.proxy and not (expected_ip or expected_country):
        pytest.skip("set WEBSKRAP_LIVE_EXPECTED_PUBLIC_IP or WEBSKRAP_LIVE_EXPECTED_COUNTRY")


async def test_device_and_browser_info_behavioral() -> None:
    # deviceandbrowserinfo.com behavioral detection: isBot must be false.
    async with stealth_page() as page:
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
                return {isBot: signals.isBot ?? null, signals};
            }"""
        )
    assert result["isBot"] is False, f"detected as bot; signals: {result['signals']}"


async def test_bot_sannysoft() -> None:
    # bot.sannysoft.com — no detection row may be marked failed.
    async with stealth_page() as page:
        await page.goto("https://bot.sannysoft.com", wait_until="networkidle", timeout=60_000)
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
    assert failed == [], f"Sannysoft failures: {', '.join(failed)}"


async def test_bot_incolumitas() -> None:
    # bot.incolumitas.com — only network/spec false positives are tolerated.
    known_acceptable = {"WEBDRIVER", "connectionRTT"}
    async with stealth_page() as page:
        await page.goto("https://bot.incolumitas.com", wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(13_000)
        failed = await page.evaluate(
            """() => {
                const text = document.body.innerText;
                const fails = text.match(/"(\\w+)":\\s*"FAIL"/g) || [];
                return fails.map(m => m.match(/"(\\w+)"/)[1]);
            }"""
        )
    unexpected = [name for name in failed if name not in known_acceptable]
    assert unexpected == [], f"Incolumitas unexpected failures: {', '.join(unexpected)}"


async def test_are_you_headless() -> None:
    # antoinevastel "are you headless" — must report not Chrome headless.
    async with stealth_page() as page:
        await page.goto(
            "https://arh.antoinevastel.com/bots/areyouheadless",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await page.wait_for_timeout(5_000)
        text = await page.evaluate("() => document.body.innerText")
    assert "You are not Chrome headless" in text, "flagged as Chrome headless"


# CreepJS renders its automation signals as boolean rows inside a labelled
# block (e.g. "Headless | webDriverIsOn: false | ..."). A clean fingerprint has
# every Headless and Stealth signal false: not detected as headless, and no
# stealth/tampering lies (proxy, patched runtime, toString proxy, faked WebGL).
# The soft "Like Headless" heuristic is intentionally ignored — it reflects
# benign environment quirks (color scheme, missing experimental APIs).
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


async def test_creepjs() -> None:
    async with stealth_page() as page:
        await page.goto(
            "https://abrahamjuliot.github.io/creepjs/",
            wait_until="networkidle",
            timeout=60_000,
        )
        await page.wait_for_timeout(16_000)
        signals = await page.evaluate(_CREEPJS_EVAL)
    assert signals["headless"], "CreepJS Headless block did not render"
    assert signals["stealth"], "CreepJS Stealth block did not render"
    assert ": true" not in signals["headless"], (
        f"CreepJS flagged a headless signal: {signals['headless']}"
    )
    assert ": true" not in signals["stealth"], (
        f"CreepJS flagged a stealth/tampering signal: {signals['stealth']}"
    )
