from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Page

from webskrap import SessionConfig, WebSkrapClient
from webskrap.challenges import raise_for_challenge
from webskrap.errors import ErrorCode, WebSkrapError


async def test_cloudflare_header_blocks_without_reading_page() -> None:
    page = MagicMock(spec=Page)
    page.evaluate = AsyncMock()
    with pytest.raises(WebSkrapError) as error:
        await raise_for_challenge(page, {"CF-Mitigated": "Challenge"})
    assert error.value.code is ErrorCode.BLOCKED
    page.evaluate.assert_not_awaited()


@pytest.mark.browser
@pytest.mark.parametrize(
    ("html", "provider"),
    [
        ('<script src="https://ct.captcha-delivery.com/c.js"></script>', "DataDome"),
        (
            '<iframe src="https://geo.captcha-delivery.com/captcha/?cid=secret"></iframe>',
            "DataDome",
        ),
        ('<iframe src="//geo.captcha-delivery.com/interstitial/"></iframe>', "DataDome"),
        (
            "<title>Just a moment...</title>"
            '<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1"></script>',
            "Cloudflare",
        ),
        ("<p>DataDome, Cloudflare, CAPTCHA and access denied troubleshooting</p>", None),
        ('<script src="https://js.datadome.co/tags.js"></script>', None),
        ('<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>', None),
        ('<script src="/cdn-cgi/challenge-platform/h/g/jsd/oneshot"></script>', None),
        ('<script src="https://ct.captcha-delivery.com.example.test/c.js"></script>', None),
        ('<iframe src="https://geo.captcha-delivery.com.example.test/captcha/"></iframe>', None),
        ('<p>&lt;script src="https://ct.captcha-delivery.com/c.js"&gt;&lt;/script&gt;</p>', None),
    ],
)
async def test_active_dom_markers_are_narrow(html, provider, sandbox_supported) -> None:
    config = SessionConfig(chromium_sandbox=sandbox_supported)
    async with WebSkrapClient(default_config=config) as client:
        session = await client.session("markers")
        # No third-party request may leave the controlled test page.
        await session.context.route("**/*", lambda route: route.abort())
        page = await session.context.new_page()
        await page.set_content('<base href="https://example.test/">' + html)
        if provider:
            with pytest.raises(WebSkrapError) as error:
                await raise_for_challenge(page, {})
            assert error.value.code is ErrorCode.BLOCKED
            assert provider in str(error.value)
            assert "secret" not in str(error.value)
        else:
            await raise_for_challenge(page, {})
