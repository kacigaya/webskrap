from __future__ import annotations

import asyncio
from typing import Any

from webskrap import mcp_server
from webskrap.models import FetchResult


class _FakeClient:
    calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        return None

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append({"url": url, **kwargs})
        text = "Readable body" if kwargs.get("text_only") else "<html>abcdef</html>"
        return FetchResult(
            url=url,
            final_url=f"{url}/final",
            status=200,
            ok=True,
            headers={"content-type": "text/html"},
            text=text,
            title="Example",
            cookies=[],
            timings={"elapsed_ms": 12.34},
        )


def _fake_client(monkeypatch: Any) -> None:
    _FakeClient.calls = []
    monkeypatch.setattr(mcp_server, "WebSkrapClient", _FakeClient)


def test_fetch_defaults_to_clean_text(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = asyncio.run(mcp_server.fetch("https://example.test"))

    assert _FakeClient.calls[0]["text_only"] is True
    assert result["text"] == "Readable body"


def test_fetch_uses_stealth_driver_by_default(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    asyncio.run(mcp_server.fetch("https://example.test"))

    config = _FakeClient.calls[0]["config"]
    assert config.driver == "patchright"
    assert config.channel == "chrome"
    assert config.headless is True
    assert _FakeClient.calls[0]["wait_until"] == "networkidle"


def test_fetch_text_only_false_returns_html(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = asyncio.run(mcp_server.fetch("https://example.test", text_only=False))

    assert _FakeClient.calls[0]["text_only"] is False
    assert result["text"] == "<html>abcdef</html>"


def test_stealth_fetch_defaults_to_clean_text(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = asyncio.run(mcp_server.stealth_fetch("https://example.test"))

    assert _FakeClient.calls[0]["text_only"] is True
    assert result["text"] == "Readable body"
