from __future__ import annotations

import asyncio

import pytest

from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.urls import (
    PRIVATE_NET_ENV,
    private_net_allowed,
    reject_private_network,
    validate_mcp_url,
    validate_url,
)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://example.com/", id="https"),
        pytest.param("http://example.com:8080/path?q=1", id="http-port-path"),
        pytest.param("  https://example.com/  ", id="whitespace-stripped"),
        pytest.param("HTTP://example.com/", id="uppercase-scheme"),
        pytest.param("data:text/html,<title>t</title>", id="data-url"),
        pytest.param("about:blank", id="about-blank"),
    ],
)
def test_validate_url_accepts_fetchable_targets(url: str) -> None:
    assert validate_url(url) == url.strip()


@pytest.mark.parametrize(
    ("url", "match"),
    [
        pytest.param("file:///etc/passwd", "scheme must be http", id="file"),
        pytest.param("ftp://example.com/x", "scheme must be http", id="ftp"),
        pytest.param("javascript:alert(1)", "scheme must be http", id="javascript"),
        pytest.param("https://user:pass@example.com/", "credentials", id="userinfo"),
        pytest.param("https://user@example.com/", "credentials", id="user-only"),
        pytest.param("https://", "no host", id="no-host"),
        pytest.param("", "scheme must be http", id="empty"),
    ],
)
def test_validate_url_rejects_unfetchable_targets(url: str, match: str) -> None:
    with pytest.raises(WebSkrapError, match=match):
        validate_url(url)


def test_validate_url_rejection_is_usage() -> None:
    with pytest.raises(WebSkrapError) as caught:
        validate_url("file:///etc/passwd")

    assert caught.value.code is ErrorCode.USAGE


def test_validate_url_rejects_malformed_urls() -> None:
    with pytest.raises(WebSkrapError, match="invalid URL"):
        validate_url("http://[::1")


def test_private_net_opt_out_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PRIVATE_NET_ENV, raising=False)

    assert private_net_allowed() is False


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_private_net_opt_in_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(PRIVATE_NET_ENV, value)

    assert private_net_allowed() is True


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://127.0.0.1:8000/", id="loopback-v4"),
        pytest.param("http://[::1]/", id="loopback-v6"),
        pytest.param("http://10.0.0.1/", id="rfc1918-10"),
        pytest.param("http://192.168.1.1/", id="rfc1918-192"),
        pytest.param("http://172.16.0.1/", id="rfc1918-172"),
        pytest.param("http://169.254.169.254/", id="link-local"),
        pytest.param("http://localhost:8000/", id="localhost-name"),
        pytest.param("http://app.localhost/", id="localhost-tld"),
        pytest.param("http://[::]/", id="unspecified"),
    ],
)
def test_reject_private_network_blocks_non_public_hosts(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.delenv(PRIVATE_NET_ENV, raising=False)

    with pytest.raises(WebSkrapError, match="private or local hosts are blocked"):
        asyncio.run(reject_private_network(url))


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://8.8.8.8/", id="public-v4"),
        pytest.param("data:text/html,<title>t</title>", id="data-url"),
        pytest.param("about:blank", id="about-blank"),
    ],
)
def test_reject_private_network_allows_public_and_non_network(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.delenv(PRIVATE_NET_ENV, raising=False)

    asyncio.run(reject_private_network(url))


def test_reject_private_network_unresolvable_host_defers_to_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # .invalid never resolves: the DNS failure must not become a rejection,
    # or every typo would misreport as a policy block.
    monkeypatch.delenv(PRIVATE_NET_ENV, raising=False)

    asyncio.run(reject_private_network("https://nonexistent.invalid/"))


def test_reject_private_network_opt_out_allows_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PRIVATE_NET_ENV, "1")

    asyncio.run(reject_private_network("http://127.0.0.1:8000/"))


def test_private_block_is_usage_with_opt_out_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PRIVATE_NET_ENV, raising=False)

    with pytest.raises(WebSkrapError) as caught:
        asyncio.run(reject_private_network("http://127.0.0.1/"))

    assert caught.value.code is ErrorCode.USAGE
    assert PRIVATE_NET_ENV in str(caught.value)


def test_validate_mcp_url_combines_both_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PRIVATE_NET_ENV, raising=False)

    with pytest.raises(WebSkrapError):
        asyncio.run(validate_mcp_url("file:///etc/passwd"))
    with pytest.raises(WebSkrapError):
        asyncio.run(validate_mcp_url("http://169.254.169.254/"))

    assert asyncio.run(validate_mcp_url("https://example.com/")) == "https://example.com/"
