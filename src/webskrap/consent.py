"""Cookie-consent notice auto-decline.

Inspired by Brave's [cookiecrumbler](https://github.com/brave/cookiecrumbler),
which *detects* consent notices. WebSkrap goes one step further and clicks the
notice's reject control, so scraped text is not buried under a banner and no
optional cookies get accepted.

Two strategies, tried in order inside every frame of the page:

1. ``CMP_REJECT_CSS`` — reject buttons of the widely deployed consent platforms
   (OneTrust, Cookiebot, Didomi, Usercentrics, ...). Exact, no guessing.
2. Text match — a clickable whose label matches ``REJECT_TEXT_PATTERN``, scoped
   to a cookie/consent-looking container so "Decline" buttons elsewhere on the
   page are never touched.
"""

from __future__ import annotations

import re
from contextlib import suppress
from time import monotonic
from typing import Any

# Reject/deny buttons of common consent management platforms.
CMP_REJECT_SELECTORS = (
    "#onetrust-reject-all-handler",
    ".ot-pc-refuse-all-handler",
    "#CybotCookiebotDialogBodyButtonDecline",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll",
    "#didomi-notice-disagree-button",
    ".didomi-continue-without-agreeing",
    "[data-testid='uc-deny-all-button']",
    "#uc-btn-deny-banner",
    ".qc-cmp2-summary-buttons button[mode='secondary']",
    "button.sp_choice_type_REJECT_ALL",
    ".osano-cm-denyAll",
    "[data-tid='banner-decline']",
    ".cmplz-deny",
    ".cky-btn-reject",
    ".cm-btn-decline",
    ".cn-decline",
    "#cookiescript_reject",
    "[data-cookie-refuse]",
    ".iubenda-cs-reject-btn",
    "#truste-consent-required",
    "#axeptio_btn_dismiss",
    "#tarteaucitronAllDenied2",
    ".tarteaucitronDeny",
    "#cn-refuse-cookie",
    "#wt-cli-reject-btn",
    ".fc-cta-do-not-consent",
    "[data-cookiefirst-action='reject']",
    ".cc-nb-reject",
    ".cc-deny",
)
CMP_REJECT_CSS = ", ".join(CMP_REJECT_SELECTORS)

# Containers that look like a cookie notice. Used to scope the text strategy.
CONSENT_CONTAINER_CSS = ", ".join(
    (
        '[id*="cookie" i]',
        '[class*="cookie" i]',
        '[id*="consent" i]',
        '[class*="consent" i]',
        '[id*="gdpr" i]',
        '[class*="gdpr" i]',
        '[aria-label*="cookie" i]',
    )
)

# CMPs that render inside an iframe (Sourcepoint, TrustArc, Quantcast, ...).
# The <iframe> itself lives in the main document, so waiting on it works.
CONSENT_IFRAME_CSS = ", ".join(
    (
        'iframe[id*="sp_message" i]',
        'iframe[id*="consent" i]',
        'iframe[title*="consent" i]',
        'iframe[src*="consensu.org" i]',
        'iframe[src*="trustarc" i]',
    )
)

CONSENT_PRESENT_CSS = f"{CMP_REJECT_CSS}, {CONSENT_CONTAINER_CSS}, {CONSENT_IFRAME_CSS}"

# Frames served by a consent platform. Their whole document *is* the notice, so
# the text strategy skips the container scoping there (Sourcepoint and friends
# do not put "cookie" or "consent" in their inner markup).
CONSENT_FRAME_URL_PATTERN = re.compile(
    "|".join(
        (
            "consent",
            "sourcepoint",
            "privacy-mgmt",
            "consensu",
            "trustarc",
            "didomi",
            "onetrust",
            "cookielaw",
            "cookiebot",
            "quantcast",
            "usercentrics",
            "cookieyes",
            "axeptio",
            "iubenda",
            "sp_message",
        )
    ),
    re.IGNORECASE,
)

CLICKABLE_CSS = "button, [role='button'], a, input[type='button'], input[type='submit']"

# Reject wording across the languages WebSkrap profiles ship locales for. No
# \b anchors: they do not work for CJK, and the match is already scoped to a
# consent container.
REJECT_TEXT_PATTERN = re.compile(
    "|".join(
        (
            r"reject",
            r"declin",
            r"refuse",
            r"deny",
            r"disagree",
            r"do ?n[o']t accept",
            r"only (?:essential|necessary|required|strictly)",
            r"(?:essential|necessary|required)[a-z ]* only",
            r"continue without accepting",
            r"refuser",
            r"continuer sans accepter",
            r"ablehnen",
            r"nur (?:notwendige|essenzielle|erforderliche)",
            r"rechazar",
            r"solo (?:las )?necesarias",
            r"rejeitar",
            r"recusar",
            r"apenas (?:as )?essenciais",
            r"rifiuta",
            r"continua senza accettare",
            r"weigeren",
            r"afwijzen",
            r"odrzu",
            r"avvis",
            r"afvis",
            r"avslå",
            r"hylkää",
            r"odmítnout",
            r"reddet",
            r"отклонить",  # otklonit
            r"拒否",  # ja
            r"拒绝",  # zh-hans
            r"拒絕",  # zh-hant
            r"거부",  # ko
        )
    ),
    re.IGNORECASE,
)

# A banner has a handful of buttons at most; scanning more means we matched the
# wrong container.
MAX_CANDIDATES = 6
CLICK_TIMEOUT_MS = 1_500
# Time for the notice to animate away before the page is read.
SETTLE_MS = 300
# A consent platform's iframe is visible before its contents finish rendering,
# so retry while one is attached. Only pages that actually carry such a frame
# pay this; everything else gets a single pass.
# ponytail: fixed 3s poll ceiling for iframe render; wait on the reject locator
# inside the frame if slow CMPs start slipping through
POLL_INTERVAL_MS = 250
POLL_ATTEMPTS = 12
# Hard cap on everything after detection (polling plus click attempts), so the
# total cost stays timeout_ms + this instead of growing with candidate count.
POST_DETECT_BUDGET_MS = 3_000
# A caller that already waited for networkidle gave CMP scripts their chance to
# inject, so the detection wait on top of that is mostly dead time.
SETTLED_PAGE_TIMEOUT_MS = 500


async def decline_cookies(page: Any, *, timeout_ms: float = 2_000) -> str | None:
    """Click the reject control of a cookie consent notice, if one shows up.

    Waits up to ``timeout_ms`` for a consent notice to appear (CMP scripts
    usually inject it after DOMContentLoaded), then tries every frame. Returns
    the strategy that clicked (``"cmp"`` or ``"text"``), or ``None`` when no
    notice was found. Never raises: a page without a notice, or a notice this
    cannot handle, is not an error.

    Total wall time is bounded by ``timeout_ms + POST_DETECT_BUDGET_MS``.

    ponytail: only the notice's first layer is handled. Walls with no reject
    control there (zeit.de and other pay-or-consent publishers) are left alone,
    and walls that answer the reject click with a subscription upsell
    (theguardian.com) stay up -- the return value reports the click, not a clean
    page. Walking further needs per-CMP second-layer flows.
    """
    deadline = monotonic() + (max(timeout_ms, 0.0) + POST_DETECT_BUDGET_MS) / 1000
    if timeout_ms > 0:
        try:
            await page.wait_for_selector(
                f"{CONSENT_PRESENT_CSS} >> visible=true", timeout=timeout_ms
            )
        except Exception:  # noqa: BLE001 - no notice appeared, nothing to decline
            return None

    for attempt in range(POLL_ATTEMPTS):
        for frame in page.frames:
            strategy = await _decline_in_frame(frame, deadline)
            if strategy is not None:
                with suppress(Exception):  # settle wait is best-effort
                    await page.wait_for_timeout(SETTLE_MS)
                return strategy
        # Nothing left that can still render into a notice, or out of budget.
        if (
            timeout_ms <= 0
            or attempt + 1 == POLL_ATTEMPTS
            or monotonic() >= deadline
            or not _has_consent_frame(page)
        ):
            break
        with suppress(Exception):
            await page.wait_for_timeout(POLL_INTERVAL_MS)
    return None


def _has_consent_frame(page: Any) -> bool:
    try:
        return any(CONSENT_FRAME_URL_PATTERN.search(frame.url or "") for frame in page.frames)
    except Exception:  # noqa: BLE001 - frames detach mid-iteration
        return False


def _is_consent_frame(frame: Any) -> bool:
    return bool(CONSENT_FRAME_URL_PATTERN.search(getattr(frame, "url", "") or ""))


async def _decline_in_frame(frame: Any, deadline: float) -> str | None:
    text_scope = frame if _is_consent_frame(frame) else frame.locator(CONSENT_CONTAINER_CSS)
    candidates = (
        ("cmp", frame.locator(CMP_REJECT_CSS)),
        ("text", text_scope.locator(CLICKABLE_CSS).filter(has_text=REJECT_TEXT_PATTERN)),
    )
    for strategy, locator in candidates:
        try:
            total = min(await locator.count(), MAX_CANDIDATES)
        except Exception:  # noqa: BLE001 - detached frame or bad selector engine
            return None
        for index in range(total):
            click_timeout = min(CLICK_TIMEOUT_MS, (deadline - monotonic()) * 1000)
            if click_timeout <= 0:
                return None
            element = locator.nth(index)
            try:
                if not await element.is_visible():
                    continue
                # Playwright dispatches real browser input events here, not a
                # JavaScript el.click(), so this is not a synthesized-event
                # tell. Deliberately not human_click: that takes a Page and
                # notices live in frames, and consent widgets do not score
                # cursor trajectory.
                await element.click(timeout=click_timeout)
            # S112/BLE001: one unclickable candidate (covered, detached, or the
            # page already navigating) must not abandon the remaining ones, and
            # the library logs nothing by design.
            except Exception:  # nosec B112  # noqa: BLE001, S112
                continue
            return strategy
    return None
