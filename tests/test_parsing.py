from __future__ import annotations

import pytest

from webskrap.models import ResourcePolicy
from webskrap.parsing import (
    parse_resource_policy,
    parse_wait_until,
    parse_webrtc_ip_handling_policy,
)


@pytest.mark.parametrize("value", ["commit", "domcontentloaded", "load", "networkidle"])
def test_parse_wait_until_accepts_playwright_states(value: str) -> None:
    assert parse_wait_until(value) == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("sometime", id="unknown"),
        pytest.param("", id="empty"),
        pytest.param("LOAD", id="wrong-case"),
    ],
)
def test_parse_wait_until_rejects_other_values(value: str) -> None:
    with pytest.raises(ValueError, match="wait_until must be one of"):
        parse_wait_until(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("all", ResourcePolicy.ALL, id="all"),
        pytest.param("lite", ResourcePolicy.LITE, id="lite"),
        pytest.param("documents", ResourcePolicy.DOCUMENTS, id="documents"),
    ],
)
def test_parse_resource_policy(value: str, expected: ResourcePolicy) -> None:
    assert parse_resource_policy(value) is expected


@pytest.mark.parametrize("value", ["none", "", "LITE"])
def test_parse_resource_policy_rejects_other_values(value: str) -> None:
    with pytest.raises(ValueError, match="resource_policy must be one of"):
        parse_resource_policy(value)


@pytest.mark.parametrize(
    "value",
    [
        "default",
        "default_public_and_private_interfaces",
        "default_public_interface_only",
        "disable_non_proxied_udp",
    ],
)
def test_parse_webrtc_policy_accepts_chromium_values(value: str) -> None:
    assert parse_webrtc_ip_handling_policy(value) == value


def test_parse_webrtc_policy_passes_none_through() -> None:
    assert parse_webrtc_ip_handling_policy(None) is None


@pytest.mark.parametrize("value", ["disabled", "", "Default"])
def test_parse_webrtc_policy_rejects_other_values(value: str) -> None:
    with pytest.raises(ValueError, match="webrtc_ip_handling_policy must be one of"):
        parse_webrtc_ip_handling_policy(value)
