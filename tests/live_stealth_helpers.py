from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any

import pytest

from webskrap import GpuBackend, ProxyConfig, WebSkrapSession
from webskrap.cdp import CDPPage
from webskrap.cdp_dom import snapshot
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


@dataclass(frozen=True)
class LiveResponse:
    status: int
    headers: dict[str, str]


class LivePage:
    """The few page calls the live tests make, on a session's CDP page."""

    def __init__(self, session: WebSkrapSession, page: CDPPage) -> None:
        self.session = session
        self.page = page

    async def goto(
        self, url: str, *, wait_until: str = "load", timeout: float = 30_000
    ) -> LiveResponse | None:
        response = await self.page.goto(url, wait_until=wait_until, timeout_s=timeout / 1000)
        if not response:
            return None
        return LiveResponse(int(response.get("status", 0)), dict(response.get("headers", {})))

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        return await self.page.evaluate(script, arg)

    async def wait_for_function(self, script: str, *, timeout: float = 30_000) -> None:
        """Poll ``script`` until it returns a truthy value.

        Raises:
            TimeoutError: If it stays falsy for ``timeout`` milliseconds.
        """
        async with asyncio.timeout(timeout / 1000):
            while not await self.page.evaluate(script):
                await asyncio.sleep(0.25)

    async def wait_for_timeout(self, timeout: float) -> None:
        await asyncio.sleep(timeout / 1000)

    async def click_button(self, name: str, *, timeout: float = 10_000) -> None:
        """Click the first button named ``name`` in the snapshot, as a person would."""
        async with asyncio.timeout(timeout / 1000):
            while True:
                pattern = rf'button "{re.escape(name)}".*\[ref=(e\d+)\]'
                match = re.search(pattern, await snapshot(self.page))
                if match:
                    break
                await asyncio.sleep(0.25)
        await self.session.human_click(self.page, match.group(1), timeout=timeout)


async def goto_or_skip(page, url: str, **kwargs):
    # Known challenges are detection verdicts even when served with a 5xx.
    # Only skip a service error once the provider checks have ruled them out.
    response = await page.goto(url, **kwargs)
    await raise_for_challenge(
        page,
        response.headers if response is not None else {},
        response.status if response is not None else None,
    )
    if response is not None and response.status >= 500:
        pytest.skip(f"{url} returned HTTP {response.status}")
    return response


async def wait_for_recaptcha_score_or_skip(page: LivePage) -> None:
    try:
        await page.wait_for_function(
            """() => /"score":\\s*\\d+\\.\\d+/.test(document.body.innerText)""",
            timeout=45_000,
        )
    except TimeoutError:
        text = await page.evaluate("() => document.body.innerText")
        if "grecaptcha.ready() fired" in text and "grecaptcha.execute" in text:
            pytest.skip("reCAPTCHA demo did not return a score after execute()")
        raise
