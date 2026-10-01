"""Recognize active challenge pages without solving them or copying their tokens.

These checks deliberately use provider-specific elements and headers. Mentioning
a CAPTCHA in an article, embedding Turnstile, or receiving a plain HTTP 403 does
not establish that a provider replaced the requested page with a challenge.
"""

from __future__ import annotations

from collections.abc import Mapping

from playwright.async_api import Page

from webskrap.errors import ErrorCode, WebSkrapError

# Return only a fixed provider name, never page text, URLs, or challenge tokens.
# Reading the DOM does not override any browser fingerprint surface.
_CHALLENGE_SCRIPT = r"""() => {
  for (const element of document.querySelectorAll('script[src], iframe[src]')) {
    let url;
    try { url = new URL(element.getAttribute('src'), document.baseURI); }
    catch { continue; }
    if (element.tagName === 'SCRIPT' && url.hostname === 'ct.captcha-delivery.com'
        && url.pathname === '/c.js') return 'DataDome';
    if (element.tagName === 'IFRAME' && url.hostname === 'geo.captcha-delivery.com'
        && /^\/(captcha|interstitial)\//.test(url.pathname)) return 'DataDome';
    if (element.tagName === 'SCRIPT'
        && url.pathname.startsWith('/cdn-cgi/challenge-platform/')
        && /^Just a moment[.!\s]*$/i.test(document.title.trim())) return 'Cloudflare';
  }
  return null;
}"""


async def raise_for_challenge(page: Page, headers: Mapping[str, str]) -> None:
    """Raise ``blocked`` for a recognized DataDome or Cloudflare challenge.

    Ordinary error pages and embedded verification widgets are left alone.
    Detection is a snapshot, not a guarantee that no later challenge will appear.
    The exception contains no page-controlled text or session tokens.
    """
    if any(
        name.lower() == "cf-mitigated" and value.lower() == "challenge"
        for name, value in headers.items()
    ):
        provider = "Cloudflare"
    else:
        provider = await page.evaluate(_CHALLENGE_SCRIPT)
    if provider in ("DataDome", "Cloudflare"):
        raise WebSkrapError(
            f"{provider} served a bot challenge instead of the requested page",
            code=ErrorCode.BLOCKED,
        )
