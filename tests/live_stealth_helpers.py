from __future__ import annotations

import os

import pytest

from webskrap import GpuBackend, ProxyConfig


def live_proxy() -> ProxyConfig | None:
    server = os.environ.get("WEBSKRAP_LIVE_PROXY")
    if not server:
        return None
    return ProxyConfig(
        server=server,
        username=os.environ.get("WEBSKRAP_LIVE_PROXY_USERNAME"),
        password=os.environ.get("WEBSKRAP_LIVE_PROXY_PASSWORD"),
    )


def live_gpu() -> GpuBackend:
    # GPU-less hosts have no WebGL in headed Chromium; WEBSKRAP_LIVE_GPU=mesa
    # renders it through lavapipe so WebGL checks measure WebSkrap, not the host.
    return GpuBackend(os.environ.get("WEBSKRAP_LIVE_GPU", GpuBackend.AUTO))


async def goto_or_skip(page, url: str, **kwargs):
    # A 5xx is the demo site being down, not a detection verdict. Cloudflare
    # can serve its bot challenge as a 503; that is a verdict and must fail.
    response = await page.goto(url, **kwargs)
    if (
        response is not None
        and response.status >= 500
        and response.headers.get("cf-mitigated") != "challenge"
    ):
        pytest.skip(f"{url} returned HTTP {response.status}")
    return response


async def wait_for_recaptcha_score_or_skip(page) -> None:
    try:
        await page.wait_for_function(
            """() => /"score":\\s*\\d+\\.\\d+/.test(document.body.innerText)""",
            timeout=45_000,
        )
    except Exception as exc:  # noqa: BLE001 - patchright has its own TimeoutError type
        if exc.__class__.__name__ != "TimeoutError":
            raise
        text = await page.evaluate("() => document.body.innerText")
        if "grecaptcha.ready() fired" in text and "grecaptcha.execute" in text:
            pytest.skip("reCAPTCHA demo did not return a score after execute()")
        raise
