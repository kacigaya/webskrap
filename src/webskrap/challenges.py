"""Recognize challenge and block pages without solving them or copying their tokens.

These checks deliberately use provider-specific elements and headers. Mentioning
a CAPTCHA in an article, embedding Turnstile, or receiving a plain HTTP 403 does
not establish that a provider replaced the requested page.

Two kinds are told apart, because they need different fixes:

- A *challenge* asks the browser to prove itself (DataDome, Cloudflare's "Just a
  moment", Akamai's sec-cpt page, AWS WAF's challenge action). A more
  browser-like run can pass it.
- A *block* refuses the client outright (a Cloudflare block page, an Akamai
  edge reference page). Only a different exit IP or fingerprint helps.

A block is reported only when the provider's own response markers agree with
the page: Cloudflare's ``server`` header with its error-page structure or the
response's Ray ID, or Akamai's ``AkamaiGHost`` server with an edge reference
number. An origin's own 403 or 404 behind the same CDN carries neither.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from webskrap.cdp import CDPError, CDPPage, EventRecorder
from webskrap.errors import ErrorCode, WebSkrapError

BlockKind = Literal["challenge", "block"]

#: How long a self-clearing interstitial gets to reload into the requested page.
INTERSTITIAL_TIMEOUT_S = 15.0
_POLL_S = 0.25


class ScriptRunner(Protocol):
    """Anything that runs a page script and returns its JSON value."""

    async def evaluate(self, expression: str, arg: Any = None, /) -> Any:
        """Run ``expression`` with ``arg`` and return its JSON value."""
        ...


@dataclass(frozen=True)
class Detection:
    """A recognized challenge or block page. Holds no page-controlled text."""

    provider: str
    kind: BlockKind
    #: True for interstitials whose own script reloads into the page.
    reloads: bool = False

    @property
    def message(self) -> str:
        """Return the one-line error message for this detection."""
        if self.kind == "block":
            return f"{self.provider} blocked this client instead of serving the requested page"
        return f"{self.provider} served a bot challenge instead of the requested page"

    def hint(self, *, browser_session: bool = False) -> str:
        """Return the recovery hint; ``browser_session`` targets the browser tools."""
        if browser_session:
            if self.kind == "block":
                return "Reopen the session through another exit IP (webskrap browser open --proxy)."
            return (
                "Solve it in a headed session (webskrap browser open --headed) and keep the "
                "profile. WebSkrap does not solve CAPTCHAs."
            )
        return blocked_hint(self.kind)

    def payload(self) -> dict[str, str]:
        """Return the machine-readable form the browser tools report."""
        return {
            "code": str(ErrorCode.BLOCKED),
            "provider": self.provider,
            "kind": self.kind,
            "hint": self.hint(browser_session=True),
        }

    def error(self) -> WebSkrapError:
        """Return the ``blocked`` error for this detection."""
        return WebSkrapError(self.message, code=ErrorCode.BLOCKED, hint=self.hint())


def xvfb_available() -> bool:
    """True on Linux when an Xvfb binary is on PATH, so virtual_display can start."""
    return sys.platform.startswith("linux") and shutil.which("Xvfb") is not None


def blocked_hint(kind: BlockKind) -> str:
    """Return the fetch-tool recovery hint for a ``kind`` detection on this host.

    ``virtual_display=True`` comes first where it can run: a headed browser on
    a private Xvfb display passed checks that headless and locale profiles did
    not, at no cost in exit IPs.
    """
    if kind == "block":
        if xvfb_available():
            return (
                "Try virtual_display=True (Xvfb is installed), or change the exit IP (proxy). "
                "Retrying unchanged will not help."
            )
        return (
            "Change the exit IP (proxy), or try virtual_display=True on Linux with Xvfb. "
            "Retrying unchanged will not help."
        )
    if xvfb_available():
        return (
            "Try virtual_display=True (Xvfb is installed), headed mode, or a persistent "
            "user_data_dir. WebSkrap does not solve CAPTCHAs."
        )
    return (
        "Try headed mode, virtual_display=True (Linux with Xvfb), or a persistent "
        "user_data_dir. WebSkrap does not solve CAPTCHAs."
    )


# Return only fixed provider names, never page text, URLs, IDs or tokens.
# An Akamai reference is <kind>.<8 hex>.<10-digit time>.<hex counter>.
# Reading the DOM does not override any browser fingerprint surface.
# Cloudflare block codes: 1005-1010 (ASN, IP, country, browser signature bans),
# 1012 (access denied), 1015 (rate limited) and 1020 (firewall rule).
_DETECT_SCRIPT = r"""({status, ray, cloudflare, akamai}) => {
  const found = (provider, kind, reloads = false) => ({provider, kind, reloads});
  for (const element of document.querySelectorAll('script[src], iframe[src]')) {
    let url;
    try { url = new URL(element.getAttribute('src'), document.baseURI); }
    catch { continue; }
    if (element.tagName === 'SCRIPT' && url.hostname === 'ct.captcha-delivery.com'
        && url.pathname === '/c.js') return found('DataDome', 'challenge');
    if (element.tagName === 'IFRAME' && url.hostname === 'geo.captcha-delivery.com'
        && /^\/(captcha|interstitial)\//.test(url.pathname)) return found('DataDome', 'challenge');
    if (element.tagName === 'SCRIPT'
        && url.pathname.startsWith('/cdn-cgi/challenge-platform/')
        && /^Just a moment[.!\s]*$/i.test(document.title.trim()))
      return found('Cloudflare', 'challenge');
  }
  if (document.getElementById('sec-if-cpt-container')) return found('Akamai', 'challenge', true);
  const text = document.body ? document.body.textContent : '';
  if (cloudflare && (status === 403 || status === 429)) {
    const codes = document.querySelectorAll(
      '#cf-error-details .cf-error-code, #cf-error-details h1 span');
    for (const code of codes) {
      if (/^10(0[5-9]|10|12|15|20)$/.test(code.textContent.trim()))
        return found('Cloudflare', 'block');
    }
    // A site's custom block page still names the Ray ID Cloudflare sent.
    if (status === 403 && ray && text.includes(ray)) return found('Cloudflare', 'block');
  }
  if (akamai && (status === 400 || status === 403)
      && /\b\d{1,3}\.[0-9a-f]{8}\.\d{10}\.[0-9a-f]{1,8}\b/.test(text))
    return found('Akamai', 'block');
  return null;
}"""


def _header(headers: Mapping[str, str], name: str) -> str:
    return next((value for key, value in headers.items() if key.lower() == name), "").strip()


def _cloudflare_ray(headers: Mapping[str, str]) -> str | None:
    """Return the Ray ID of a ``CF-Ray: <16 hex>-<colo>`` header, else None."""
    ray = _header(headers, "cf-ray").split("-", 1)[0].lower()
    return ray if len(ray) == 16 and all(c in "0123456789abcdef" for c in ray) else None


async def detect(
    page: ScriptRunner, headers: Mapping[str, str], status: int | None = None
) -> Detection | None:
    """Return the challenge or block page ``page`` shows, or None.

    ``headers`` and ``status`` belong to the response of the document on
    screen. Detection is a snapshot, not a guarantee that no later challenge
    will appear.
    """
    if _header(headers, "cf-mitigated").lower() == "challenge":
        return Detection("Cloudflare", "challenge")
    waf_action = _header(headers, "x-amzn-waf-action").lower()
    if waf_action in ("challenge", "captcha"):
        return Detection("AWS WAF", "challenge", reloads=waf_action == "challenge")
    server = _header(headers, "server").lower()
    found = await page.evaluate(
        _DETECT_SCRIPT,
        {
            "status": status,
            "ray": _cloudflare_ray(headers),
            "cloudflare": server == "cloudflare",
            "akamai": server == "akamaighost",
        },
    )
    if not isinstance(found, dict):
        return None
    provider, kind = found.get("provider"), found.get("kind")
    if provider not in ("DataDome", "Cloudflare", "Akamai") or kind not in ("challenge", "block"):
        return None
    return Detection(provider, kind, reloads=bool(found.get("reloads")))


async def raise_for_challenge(
    page: ScriptRunner, headers: Mapping[str, str], status: int | None = None
) -> None:
    """Raise ``blocked`` for a recognized challenge or block page.

    Ordinary error pages and embedded verification widgets are left alone.
    The exception contains no page-controlled text, IPs, Ray IDs, reference
    IDs or session tokens; its hint depends on the kind.
    """
    found = await detect(page, headers, status)
    if found is not None:
        raise found.error()


def response_headers(response: Mapping[str, Any]) -> dict[str, str]:
    """Return a CDP response's headers with lowercased names."""
    return {str(key).lower(): str(value) for key, value in response.get("headers", {}).items()}


def response_status(response: Mapping[str, Any]) -> int | None:
    """Return a CDP response's status, or None for loads without one."""
    status = response.get("status")
    return int(status) if isinstance(status, int | float) else None


def record_documents(page: CDPPage) -> EventRecorder:
    """Start recording what :func:`settle` needs; start it before navigating."""
    return page.record("Page.lifecycleEvent", "Page.frameNavigated", "Network.responseReceived")


def _main_document(
    events: EventRecorder, frame_id: str
) -> tuple[str | None, dict[str, Any] | None]:
    """Return the last committed main-frame loader and its document response."""
    loader = None
    for method, params in events.events:
        frame = params.get("frame", {})
        if method == "Page.frameNavigated" and frame.get("id") == frame_id:
            loader = frame.get("loaderId")
    for method, params in reversed(events.events):
        if (
            method == "Network.responseReceived"
            and params.get("type") == "Document"
            and loader is not None
            and params.get("loaderId") == loader
        ):
            return loader, params.get("response", {})
    return loader, None


async def settle(
    page: CDPPage,
    events: EventRecorder,
    response: dict[str, Any],
    *,
    wait_until: str,
    deadline: float,
) -> dict[str, Any]:
    """Let a self-clearing interstitial reload into the requested page.

    Akamai's sec-cpt page and AWS WAF's challenge action run their own script
    and reload the URL, as they do in any browser; nothing here interacts with
    them. Waits at most :data:`INTERSTITIAL_TIMEOUT_S` and never past
    ``deadline``. ``events`` comes from :func:`record_documents`.

    Returns:
        The response of the document now on screen. When the interstitial is
        still there, that is its own response, and :func:`detect` reports it.
    """
    limit = min(deadline, time.monotonic() + INTERSTITIAL_TIMEOUT_S)
    current_loader, _ = _main_document(events, page.target_id)
    current = response
    while True:
        try:
            found = await detect(page, response_headers(current), response_status(current))
        except CDPError:
            found = Detection("", "challenge", reloads=True)  # the document is being replaced
        if found is None or not found.reloads or time.monotonic() >= limit:
            return current
        await asyncio.sleep(_POLL_S)
        loader, document = _main_document(events, page.target_id)
        if loader != current_loader and document is not None:
            current_loader, current = loader, document
            with suppress(WebSkrapError):
                await page.wait_lifecycle(events, loader, wait_until, limit, "the reloaded page")
