"""Browser navigation with challenge reporting and cookie consent."""

from __future__ import annotations

import time
from typing import Any

from webskrap import challenges
from webskrap.browser import consent
from webskrap.driver.cdp import CDPPage
from webskrap.driver.cdp_session import page_state
from webskrap.security.urls import validate_url

#: How long a browser navigation waits for a consent notice. Longer than the
#: fetch default (2 s): some CMPs inject their notice 7 s after load. The wait
#: ends as soon as a notice shows, and pages with no consent platform signal
#: stop after ``consent.NO_SIGNAL_TIMEOUT_MS``.
DECLINE_TIMEOUT_MS = 10_000


async def goto(
    page: CDPPage,
    url: str,
    wait_until: str,
    *,
    decline_cookies: bool = False,
    solve_cloudflare: bool = False,
    solve_datadome: bool = False,
    solve_perimeterx: bool = False,
    solve_recaptcha: bool = False,
) -> dict[str, Any]:
    """Navigate and return ``{"status", "url", "title", "blocked"}``.

    Uses the fetch tools' challenge and block detection, including the wait
    for a self-clearing interstitial. A detected page is reported in
    ``blocked`` rather than raised, so the session stays usable for a person
    to look at. With ``solve_cloudflare``, Cloudflare's Turnstile checkbox is
    clicked first; with ``solve_datadome``, DataDome's slider is dragged; with
    ``solve_perimeterx``, HUMAN's Press & Hold button is held; with
    ``solve_recaptcha``, a reCAPTCHA v2 or hCaptcha checkbox is clicked. With
    ``decline_cookies``, a consent notice is dismissed and
    ``cookie_notice_declined`` names the strategy that clicked, or is None.
    """
    url = validate_url(url)
    timeout_s = page.default_timeout_s
    deadline = time.monotonic() + timeout_s
    documents = challenges.record_documents(page)
    try:
        response = await page.goto(url, wait_until=wait_until, timeout_s=timeout_s)
        response = await challenges.settle(
            page,
            documents,
            response,
            wait_until=wait_until,
            deadline=deadline,
            solve_cloudflare=solve_cloudflare,
            solve_datadome=solve_datadome,
            solve_perimeterx=solve_perimeterx,
            solve_recaptcha=solve_recaptcha,
        )
    finally:
        documents.stop()
    status = challenges.response_status(response)
    found = await challenges.detect(
        page, challenges.response_headers(response), status, deadline=deadline
    )
    result: dict[str, Any] = {"status": status}
    if decline_cookies:
        result["cookie_notice_declined"] = (
            None if found else await consent.decline_cookies(page, timeout_ms=DECLINE_TIMEOUT_MS)
        )
    return {**result, **await page_state(page), "blocked": found.payload() if found else None}
