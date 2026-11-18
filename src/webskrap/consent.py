"""Compatibility import for :mod:`webskrap.browser.consent`."""

import sys
from typing import TYPE_CHECKING

from webskrap.browser import consent as _implementation

if TYPE_CHECKING:
    from webskrap.browser.consent import (
        CLICK_TIMEOUT_MS,
        CLICKABLE_CSS,
        CMP_REJECT_CSS,
        CMP_REJECT_SELECTORS,
        CONSENT_CONTAINER_CSS,
        CONSENT_FRAME_URL_PATTERN,
        CONSENT_IFRAME_CSS,
        CONSENT_PRESENT_CSS,
        MAX_CANDIDATES,
        POLL_ATTEMPTS,
        POLL_INTERVAL_MS,
        POST_DETECT_BUDGET_MS,
        REJECT_TEXT_PATTERN,
        SETTLE_MS,
        SETTLED_PAGE_TIMEOUT_MS,
        decline_cookies,
    )

__all__ = [
    "CLICKABLE_CSS",
    "CLICK_TIMEOUT_MS",
    "CMP_REJECT_CSS",
    "CMP_REJECT_SELECTORS",
    "CONSENT_CONTAINER_CSS",
    "CONSENT_FRAME_URL_PATTERN",
    "CONSENT_IFRAME_CSS",
    "CONSENT_PRESENT_CSS",
    "MAX_CANDIDATES",
    "POLL_ATTEMPTS",
    "POLL_INTERVAL_MS",
    "POST_DETECT_BUDGET_MS",
    "REJECT_TEXT_PATTERN",
    "SETTLED_PAGE_TIMEOUT_MS",
    "SETTLE_MS",
    "decline_cookies",
]

sys.modules[__name__] = _implementation
