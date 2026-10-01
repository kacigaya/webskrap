from __future__ import annotations

import os

import pytest

from webskrap import GpuBackend, ProxyConfig
from webskrap.challenges import raise_for_challenge


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
    # Known challenges are detection verdicts even when served with a 5xx.
    # Only skip a service error once the provider checks have ruled them out.
    response = await page.goto(url, **kwargs)
    await raise_for_challenge(page, response.headers if response is not None else {})
    if response is not None and response.status >= 500:
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
