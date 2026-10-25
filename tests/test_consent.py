from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import set_page_content

from webskrap import SessionConfig, WebSkrapClient
from webskrap.cdp import CDPPage
from webskrap.consent import REJECT_TEXT_PATTERN, decline_cookies

# Each control records its own click in the DOM, where an isolated-world
# evaluate can read it; input that is not trusted does not count.
_RECORD = (
    'onclick="event.isTrusted && '
    "(document.body.dataset.clicked = (document.body.dataset.clicked || '') + this.id + ' ')\""
)


@asynccontextmanager
async def _page(html: str, sandbox: bool) -> AsyncIterator[CDPPage]:
    async with WebSkrapClient(default_config=SessionConfig(chromium_sandbox=sandbox)) as client:
        session = await client.session("consent")
        page = await session.new_page()
        await set_page_content(page, html)
        yield page


async def _clicked(page: CDPPage) -> list[str]:
    return (await page.evaluate("document.body.dataset.clicked || ''")).split()


@pytest.mark.browser
async def test_clicks_known_cmp_reject_button(sandbox_supported: bool) -> None:
    html = f'<button id="onetrust-reject-all-handler" {_RECORD}>Reject</button>'
    async with _page(html, sandbox_supported) as page:
        assert await decline_cookies(page) == "cmp"
        assert await _clicked(page) == ["onetrust-reject-all-handler"]


@pytest.mark.browser
async def test_falls_back_to_reject_text_inside_consent_container(
    sandbox_supported: bool,
) -> None:
    # A "Decline" outside the notice is never a candidate.
    html = (
        f'<button id="outside" {_RECORD}>Decline invitation</button>'
        f'<div class="cookie-banner"><button id="accept" {_RECORD}>Accept all</button>'
        f'<button id="reject" {_RECORD}>Tout refuser</button></div>'
    )
    async with _page(html, sandbox_supported) as page:
        assert await decline_cookies(page) == "text"
        assert await _clicked(page) == ["reject"]


@pytest.mark.browser
async def test_skips_hidden_and_covered_candidates(sandbox_supported: bool) -> None:
    html = (
        f'<button class="cc-deny" id="hidden" style="display:none" {_RECORD}>No</button>'
        '<div style="position:relative">'
        f'<button class="cc-deny" id="covered" {_RECORD}>No</button>'
        '<div style="position:absolute;inset:0;background:#fff"></div></div>'
        f'<button class="cc-deny" id="real" {_RECORD}>No</button>'
    )
    async with _page(html, sandbox_supported) as page:
        assert await decline_cookies(page) == "cmp"
        assert await _clicked(page) == ["real"]


@pytest.mark.browser
async def test_clicks_inside_a_consent_iframe(sandbox_supported: bool) -> None:
    # Notices often live in CMP iframes; the click lands at the button's place
    # in the page, through the frame's offset.
    inner = f'<button class="cc-deny" id="framed" {_RECORD}>No</button>'.replace('"', "&quot;")
    html = (
        '<div style="height:120px"></div>'
        f'<iframe id="consent-frame" style="margin-left:80px" srcdoc="{inner}"></iframe>'
    )
    async with _page(html, sandbox_supported) as page:
        # An about:srcdoc frame does not look like a CMP's, so consent makes
        # one pass only; wait until the frame's document holds the button.
        for _ in range(100):
            if await page.evaluate(
                "!!document.getElementById('consent-frame').contentDocument"
                "?.getElementById('framed')"
            ):
                break
            await asyncio.sleep(0.05)
        assert await decline_cookies(page) == "cmp"
        clicked = await page.evaluate(
            "document.getElementById('consent-frame').contentDocument.body.dataset.clicked"
        )
        assert clicked == "framed "


@pytest.mark.browser
async def test_waits_for_a_late_notice_unless_the_timeout_is_zero(
    sandbox_supported: bool,
) -> None:
    # The notice is injected after DOMContentLoaded, as CMP scripts do.
    html = (
        f'<template id="notice"><button class="cc-deny" id="late" {_RECORD}>No</button></template>'
        "<script>setTimeout(() => document.body.append("
        "document.getElementById('notice').content.cloneNode(true)), 400)</script>"
    )
    async with _page(html, sandbox_supported) as page:
        assert await decline_cookies(page, timeout_ms=0) is None
        assert await decline_cookies(page, timeout_ms=3_000) == "cmp"
        assert await _clicked(page) == ["late"]


@pytest.mark.browser
async def test_returns_none_without_a_notice(sandbox_supported: bool) -> None:
    async with _page(f'<button id="reject" {_RECORD}>Reject</button>', sandbox_supported) as page:
        assert await decline_cookies(page, timeout_ms=300) is None
        assert await _clicked(page) == []


@pytest.mark.browser
async def test_stops_clicking_once_the_budget_is_spent(
    sandbox_supported: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = f'<button class="cc-deny" id="deny" {_RECORD}>No</button>'
    async with _page(html, sandbox_supported) as page:
        # First call sets the deadline, every later one is far past it.
        calls = iter([0.0])
        monkeypatch.setattr("webskrap.consent.monotonic", lambda: next(calls, 10_000.0))
        assert await decline_cookies(page, timeout_ms=0) is None
        assert await _clicked(page) == []


@pytest.mark.parametrize(
    "label",
    [
        "Reject all",
        "Decline",
        "Refuse all cookies",
        "Deny",
        "Continue without accepting",
        "Only essential cookies",
        "Necessary cookies only",
        "Tout refuser",
        "Alle ablehnen",
        "Rechazar todo",
        "Rejeitar tudo",
        "Rifiuta tutto",
        "Alles weigeren",
        "Odrzuć wszystko",
        "拒否する",
        "거부",
    ],
)
def test_reject_text_pattern_matches_reject_labels(label: str) -> None:
    assert REJECT_TEXT_PATTERN.search(label) is not None


@pytest.mark.parametrize(
    "label",
    ["Accept all", "Allow cookies", "Manage preferences", "Learn more", "Sign in", "OK"],
)
def test_reject_text_pattern_ignores_other_labels(label: str) -> None:
    assert REJECT_TEXT_PATTERN.search(label) is None


class _CrossSiteHandler(BaseHTTPRequestHandler):
    """Serves a page whose consent iframe comes from another site."""

    def do_GET(self) -> None:
        port = self.server.server_address[1]
        if self.path == "/consent-frame":
            # The reject control renders after the first consent pass.
            body = (
                f'<template id="t"><button class="cc-deny" id="framed" {_RECORD}>No</button>'
                "</template><script>setTimeout(() => document.body.append("
                "document.getElementById('t').content.cloneNode(true)), 900)</script>"
            )
        else:
            body = (
                f'<iframe id="consent-frame" src="http://localhost:{port}/consent-frame"></iframe>'
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_args: object) -> None:
        pass


@pytest.mark.browser
async def test_finds_a_late_button_in_a_cross_site_consent_iframe(
    sandbox_supported: bool,
) -> None:
    # 127.0.0.1 and localhost are different sites, so the iframe runs out of
    # process and is reported once, when auto-attach starts.
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CrossSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = SessionConfig(chromium_sandbox=sandbox_supported)
    try:
        async with WebSkrapClient(default_config=config) as client:
            session = await client.session("cross-site")
            page = await session.new_page()
            await page.goto(
                f"http://127.0.0.1:{server.server_port}/", wait_until="load", timeout_s=10
            )
            assert await decline_cookies(page, timeout_ms=1_000) == "cmp"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
