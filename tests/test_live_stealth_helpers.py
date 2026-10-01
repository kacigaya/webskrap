from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from live_stealth_helpers import goto_or_skip, live_gpu

from webskrap import GpuBackend
from webskrap.errors import ErrorCode, WebSkrapError


class _Page:
    def __init__(self, response, provider=None) -> None:
        self.response = response
        self.provider = provider

    async def goto(self, url: str, **kwargs):
        return self.response

    async def evaluate(self, script: str):
        return self.provider


def test_live_gpu_defaults_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEBSKRAP_LIVE_GPU", raising=False)
    assert live_gpu() is GpuBackend.AUTO


def test_live_gpu_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSKRAP_LIVE_GPU", "mesa")
    assert live_gpu() is GpuBackend.MESA


def test_live_gpu_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSKRAP_LIVE_GPU", "cuda")
    with pytest.raises(ValueError):
        live_gpu()


@pytest.mark.parametrize(
    "response",
    [None, SimpleNamespace(status=200, headers={}), SimpleNamespace(status=404, headers={})],
)
def test_goto_or_skip_returns_non_5xx(response) -> None:
    assert asyncio.run(goto_or_skip(_Page(response), "https://example.test")) is response


def test_goto_or_skip_skips_5xx() -> None:
    with pytest.raises(pytest.skip.Exception, match="HTTP 502"):
        asyncio.run(
            goto_or_skip(_Page(SimpleNamespace(status=502, headers={})), "https://example.test")
        )


def test_goto_or_skip_fails_cloudflare_challenge() -> None:
    response = SimpleNamespace(status=503, headers={"cf-mitigated": "challenge"})
    with pytest.raises(WebSkrapError) as error:
        asyncio.run(goto_or_skip(_Page(response), "https://example.test"))
    assert error.value.code is ErrorCode.BLOCKED


@pytest.mark.parametrize("status", [200, 403, 503])
def test_goto_or_skip_fails_datadome_instead_of_skipping(status: int) -> None:
    response = SimpleNamespace(status=status, headers={})
    with pytest.raises(WebSkrapError) as error:
        asyncio.run(goto_or_skip(_Page(response, "DataDome"), "https://example.test"))
    assert error.value.code is ErrorCode.BLOCKED
