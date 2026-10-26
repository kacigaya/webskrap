from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import set_page_content

from webskrap import SessionConfig, WebSkrapClient, challenges
from webskrap.cdp import CDPPage
from webskrap.challenges import Detection, blocked_hint, detect, raise_for_challenge
from webskrap.errors import ErrorCode, WebSkrapError, error_payload, tool_message

BLOCKS = Path(__file__).parent / "fixtures" / "blocks"
CLOUDFLARE = {"server": "cloudflare", "cf-ray": "0123456789abcdef-CDG"}
AKAMAI = {"server": "AkamaiGHost"}
# Redacted values the fixtures carry; no error may repeat them.
FIXTURE_SECRETS = ("192.0.2.1", "0123456789abcdef", "1a2b3c4d", "secret")


async def test_cloudflare_header_blocks_without_reading_page() -> None:
    page = MagicMock(spec=CDPPage)
    page.evaluate = AsyncMock()
    with pytest.raises(WebSkrapError) as error:
        await raise_for_challenge(page, {"CF-Mitigated": "Challenge"})
    assert error.value.code is ErrorCode.BLOCKED
    page.evaluate.assert_not_awaited()


@pytest.mark.parametrize(("action", "reloads"), [("challenge", True), ("captcha", False)])
async def test_aws_waf_action_header_is_a_challenge(action: str, reloads: bool) -> None:
    page = MagicMock(spec=CDPPage)
    page.evaluate = AsyncMock()
    found = await detect(page, {"x-amzn-waf-action": action, "server": "CloudFront"}, 202)
    assert found == Detection("AWS WAF", "challenge", reloads=reloads)
    page.evaluate.assert_not_awaited()


async def test_a_202_without_the_waf_header_is_left_to_the_page() -> None:
    page = MagicMock(spec=CDPPage)
    page.evaluate = AsyncMock(return_value=None)
    assert await detect(page, {"server": "CloudFront"}, 202) is None


@pytest.mark.parametrize("xvfb", [True, False])
def test_hints_tell_blocks_from_challenges(monkeypatch: pytest.MonkeyPatch, xvfb: bool) -> None:
    monkeypatch.setattr(challenges, "xvfb_available", lambda: xvfb)
    block, challenge = blocked_hint("block"), blocked_hint("challenge")
    assert "exit IP" in block and "virtual_display=True" in block
    assert "user_data_dir" in challenge and "headed" in challenge
    assert "user_data_dir" not in block
    # virtual_display leads only where Xvfb can start it.
    assert block.startswith("Try virtual_display=True") is xvfb
    assert challenge.startswith("Try virtual_display=True") is xvfb


def test_blocked_error_carries_the_kind_specific_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(challenges, "xvfb_available", lambda: True)
    error = Detection("Akamai", "block").error()
    assert error_payload(error)["hint"] == blocked_hint("block")
    assert error_payload(error)["code"] == "blocked"
    assert tool_message(error).endswith(blocked_hint("block"))


def test_browser_payload_names_the_session_fix() -> None:
    payload = Detection("Cloudflare", "block").payload()
    assert payload == {
        "code": "blocked",
        "provider": "Cloudflare",
        "kind": "block",
        "hint": "Reopen the session through another exit IP (webskrap browser open --proxy).",
    }


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
        page = await session.new_page()
        # No third-party request may leave the controlled test page.
        await set_page_content(page, '<base href="https://example.test/">' + html)
        if provider:
            with pytest.raises(WebSkrapError) as error:
                await raise_for_challenge(page, {})
            assert error.value.code is ErrorCode.BLOCKED
            assert provider in str(error.value)
            assert "secret" not in str(error.value)
        else:
            await raise_for_challenge(page, {})


@pytest.mark.browser
@pytest.mark.parametrize(
    ("fixture", "status", "headers", "expected"),
    [
        ("cloudflare-custom-block.html", 403, CLOUDFLARE, ("Cloudflare", "block")),
        ("cloudflare-1020.html", 403, CLOUDFLARE, ("Cloudflare", "block")),
        ("akamai-reference-403.html", 403, AKAMAI, ("Akamai", "block")),
        ("akamai-invalid-url-400.html", 400, AKAMAI, ("Akamai", "block")),
        ("akamai-sec-cpt.html", 200, {}, ("Akamai", "challenge")),
        # The same pages without their provider's own response markers.
        ("cloudflare-custom-block.html", 403, {"server": "cloudflare"}, None),
        ("cloudflare-custom-block.html", 403, {"server": "nginx", "cf-ray": "x"}, None),
        ("akamai-reference-403.html", 403, {"server": "Apache"}, None),
        # Ordinary pages that must stay unblocked.
        ("api-403.html", 403, CLOUDFLARE, None),
        ("api-403.html", 403, AKAMAI, None),
        ("not-found-404.html", 404, AKAMAI, None),
        ("not-found-404.html", 404, CLOUDFLARE, None),
        ("accepted-202.html", 202, {"server": "CloudFront"}, None),
        ("article-access-denied.html", 200, CLOUDFLARE, None),
        ("article-access-denied.html", 200, AKAMAI, None),
    ],
)
async def test_block_pages_need_provider_markers(
    fixture, status, headers, expected, sandbox_supported
) -> None:
    config = SessionConfig(chromium_sandbox=sandbox_supported)
    async with WebSkrapClient(default_config=config) as client:
        session = await client.session("blocks")
        page = await session.new_page()
        await set_page_content(page, (BLOCKS / fixture).read_text(encoding="utf-8"))
        found = await detect(page, headers, status)
        assert (None if found is None else (found.provider, found.kind)) == expected
        if found is None:
            await raise_for_challenge(page, headers, status)
            return
        with pytest.raises(WebSkrapError) as error:
            await raise_for_challenge(page, headers, status)
        assert error.value.code is ErrorCode.BLOCKED
        envelope = str(error_payload(error.value))
        assert not any(secret in envelope for secret in FIXTURE_SECRETS)
        assert error.value.hint == blocked_hint(found.kind)
