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

import asyncio
import re
from contextlib import suppress
from time import monotonic

from webskrap import cdp_dom
from webskrap.cdp import CDPPage

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


# Visible elements matching a CSS list; the notice-presence check.
_VISIBLE_MATCH_SCRIPT = """(css) => {
  for (const element of document.querySelectorAll(css)) {
    const box = element.getBoundingClientRect();
    if (box.width > 0 && box.height > 0 && getComputedStyle(element).visibility !== 'hidden')
      return true;
  }
  return false;
}"""

# Reject-control candidates in one frame, at most ``max`` per strategy. The
# text strategy scopes to cookie-looking containers unless the whole frame is
# a consent platform's document.
_CANDIDATES_SCRIPT = """(options) => {
  const { strategy, cmpCss, containerCss, clickableCss, pattern, wholeFrame, max } = options;
  let found;
  if (strategy === 'cmp') {
    found = [...document.querySelectorAll(cmpCss)];
  } else {
    const scopes = wholeFrame ? [document] : [...document.querySelectorAll(containerCss)];
    const reject = new RegExp(pattern, 'i');
    found = scopes.flatMap((scope) => [...scope.querySelectorAll(clickableCss)])
      .filter((element) => reject.test(element.innerText || element.textContent || ''));
  }
  return [...new Set(found)].slice(0, max);
}"""


async def decline_cookies(page: CDPPage, *, timeout_ms: float = 2_000) -> str | None:
    """Click the reject control of a cookie consent notice, if one shows up.

    Waits up to ``timeout_ms`` for a consent notice to appear (CMP scripts
    usually inject it after DOMContentLoaded), then tries every frame,
    including cross-site iframes. Returns the strategy that clicked
    (``"cmp"`` or ``"text"``), or ``None`` when no notice was found. Never
    raises: a page without a notice, or a notice this cannot handle, is not
    an error.

    Total wall time is bounded by ``timeout_ms + POST_DETECT_BUDGET_MS``.

    ponytail: only the notice's first layer is handled. Walls with no reject
    control there (zeit.de and other pay-or-consent publishers) are left alone,
    and walls that answer the reject click with a subscription upsell
    (theguardian.com) stay up -- the return value reports the click, not a clean
    page. Walking further needs per-CMP second-layer flows.
    """
    deadline = monotonic() + (max(timeout_ms, 0.0) + POST_DETECT_BUDGET_MS) / 1000
    if timeout_ms > 0 and not await _notice_appears(page, timeout_ms):
        return None
    for attempt in range(POLL_ATTEMPTS):
        try:
            page_frames = await cdp_dom.frames(page)
        except Exception:  # noqa: BLE001 - the page navigated; nothing to decline
            return None
        for frame in page_frames:
            strategy = await _decline_in_frame(frame, deadline)
            if strategy is not None:
                await asyncio.sleep(SETTLE_MS / 1000)
                return strategy
        # Nothing left that can still render into a notice, or out of budget.
        if (
            timeout_ms <= 0
            or attempt + 1 == POLL_ATTEMPTS
            or monotonic() >= deadline
            or not any(_is_consent_frame(frame.url) for frame in page_frames)
        ):
            break
        await asyncio.sleep(POLL_INTERVAL_MS / 1000)
    return None


async def _notice_appears(page: CDPPage, timeout_ms: float) -> bool:
    """Poll the main frame until a consent notice is visible or time runs out."""
    deadline = monotonic() + timeout_ms / 1000
    while True:
        with suppress(Exception):  # a navigation can destroy the world mid-poll
            if await page.evaluate(_VISIBLE_MATCH_SCRIPT, CONSENT_PRESENT_CSS):
                return True
        if monotonic() >= deadline:
            return False
        await asyncio.sleep(0.1)


def _is_consent_frame(url: str) -> bool:
    return bool(CONSENT_FRAME_URL_PATTERN.search(url or ""))


async def _decline_in_frame(frame: cdp_dom.Frame, deadline: float) -> str | None:
    for strategy in ("cmp", "text"):
        arg = {
            "strategy": strategy,
            "cmpCss": CMP_REJECT_CSS,
            "containerCss": CONSENT_CONTAINER_CSS,
            "clickableCss": CLICKABLE_CSS,
            "pattern": REJECT_TEXT_PATTERN.pattern,
            "wholeFrame": _is_consent_frame(frame.url),
            "max": MAX_CANDIDATES,
        }
        try:
            async with cdp_dom.query_elements(frame, _CANDIDATES_SCRIPT, arg) as candidates:
                for element in candidates:
                    click_timeout = min(CLICK_TIMEOUT_MS / 1000, deadline - monotonic())
                    if click_timeout <= 0:
                        return None
                    try:
                        if not await cdp_dom.is_visible(element):
                            continue
                        # The banner is usually the first thing clicked on a
                        # page, and the page's own bot script sees every mouse
                        # event, so a teleported 2 ms click here is the one
                        # that gets scored. The humanized click refuses a
                        # covered element.
                        await cdp_dom.human_click(
                            element,
                            description="cookie notice reject control",
                            timeout_s=click_timeout,
                        )
                    # S112/BLE001: one unclickable candidate (covered, detached,
                    # or the page already navigating) must not abandon the
                    # remaining ones, and the library logs nothing by design.
                    except Exception:  # nosec B112  # noqa: BLE001, S112
                        continue
                    return strategy
        except Exception:  # noqa: BLE001 - detached frame or a world destroyed mid-query
            return None
    return None
