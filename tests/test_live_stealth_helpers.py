from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from live_stealth_helpers import goto_or_skip, live_gpu

from webskrap import GpuBackend


class _Page:
    def __init__(self, response) -> None:
        self.response = response

    async def goto(self, url: str, **kwargs):
        return self.response


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
    "response", [None, SimpleNamespace(status=200), SimpleNamespace(status=404)]
)
def test_goto_or_skip_returns_non_5xx(response) -> None:
    assert asyncio.run(goto_or_skip(_Page(response), "https://example.test")) is response


def test_goto_or_skip_skips_5xx() -> None:
    with pytest.raises(pytest.skip.Exception, match="HTTP 502"):
        asyncio.run(goto_or_skip(_Page(SimpleNamespace(status=502)), "https://example.test"))
