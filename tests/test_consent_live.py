"""Live cookie-consent test against real consent management platforms.

The CMP selectors in ``webskrap.consent`` are hardcoded and rot when platforms
rename their buttons. The unit tests use local fixtures and cannot catch that,
so this one hits real sites. It is opt-in:

    WEBSKRAP_LIVE=1 pytest tests/test_consent_live.py

Whether a notice appears at all depends on the exit IP's jurisdiction: from a
non-EU address most of these sites render nothing to decline. The test skips in
that case rather than failing on geography.
"""

from __future__ import annotations

import os
import re

import pytest

from webskrap import SessionConfig, WebSkrapClient

pytestmark = [pytest.mark.browser, pytest.mark.live]

# Public pages fronted by different consent platforms.
CONSENT_SITES = (
    "https://www.theguardian.com/international",
    "https://www.lemonde.fr/",
    "https://www.bbc.com/news",
)

# Wording that only survives in the page text when a notice went undismissed.
LEFTOVER_NOTICE_PATTERN = re.compile(
    "|".join(
        (
            r"accept all cookies",
            r"we use cookies",
            r"tout accepter",
            r"accepter et fermer",
            r"alle akzeptieren",
        )
    ),
    re.IGNORECASE,
)

LIVE_CONFIG = SessionConfig(
    driver="patchright",
    channel=os.environ.get("WEBSKRAP_BROWSER_CHANNEL", "chrome"),
    headless=True,
    navigation_timeout_ms=60_000,
)


@pytest.fixture(autouse=True)
def _require_live() -> None:
    if not os.environ.get("WEBSKRAP_LIVE"):
        pytest.skip("set WEBSKRAP_LIVE=1 to run live consent tests")


@pytest.mark.asyncio
async def test_declines_real_consent_notices() -> None:
    declined: list[str] = []
    leftover: list[str] = []

    try:
        async with WebSkrapClient() as client:
            for url in CONSENT_SITES:
                result = await client.fetch(
                    url,
                    config=LIVE_CONFIG,
                    wait_until="load",
                    text_only=True,
                    timeout_ms=60_000,
                )
                if result.cookie_notice_declined:
                    declined.append(f"{url} ({result.cookie_notice_declined})")
                elif LEFTOVER_NOTICE_PATTERN.search(result.text or ""):
                    leftover.append(url)
    except Exception as exc:  # noqa: BLE001 - environment guard, re-raised as skip
        pytest.skip(f"Playwright browser unavailable: {exc}")

    if not declined and not leftover:
        pytest.skip("no consent notice was served to this exit IP")

    # Total selector rot looks like: notices are present, none get dismissed.
    assert declined, f"no notice was declined; still showing one: {leftover}"
