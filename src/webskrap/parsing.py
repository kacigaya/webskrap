"""Parse user-supplied strings into the literal types the models expect.

Shared by the CLI and the MCP server so both reject the same values with the
same message. Every function raises ``ValueError``; presentation layers turn
that into a usage error for their medium.
"""

from __future__ import annotations

from typing import cast, get_args

from webskrap.models import (
    ElementState,
    LoadState,
    ResourcePolicy,
    WaitUntil,
    WebRtcIPHandlingPolicy,
)


def parse_literal(value: str, literal: object, name: str) -> str:
    """Check ``value`` against a ``Literal``'s members.

    Args:
        value: The string to validate.
        literal: The ``Literal`` type holding the allowed values.
        name: Option name used in the error message.

    Returns:
        ``value`` unchanged.

    Raises:
        ValueError: If ``value`` is not one of the literal's members.
    """
    valid = get_args(literal)
    if value not in valid:
        raise ValueError(f"{name} must be one of: {', '.join(valid)}")
    return value


def parse_wait_until(value: str) -> WaitUntil:
    """Parse a Playwright load state.

    Raises:
        ValueError: If ``value`` is not a valid load state.
    """
    return cast(WaitUntil, parse_literal(value, WaitUntil, "wait_until"))


def parse_load_state(value: str) -> LoadState:
    """Parse a load state a loaded page can still be waited on.

    Raises:
        ValueError: If ``value`` is not a valid load state.
    """
    return cast(LoadState, parse_literal(value, LoadState, "load_state"))


def parse_element_state(value: str) -> ElementState:
    """Parse an element state to wait for.

    Raises:
        ValueError: If ``value`` is not a valid element state.
    """
    return cast(ElementState, parse_literal(value, ElementState, "state"))


def parse_resource_policy(value: str) -> ResourcePolicy:
    """Parse a :class:`~webskrap.models.ResourcePolicy` name.

    Raises:
        ValueError: If ``value`` is not a known policy.
    """
    try:
        return ResourcePolicy(value)
    except ValueError as exc:
        allowed = ", ".join(p.value for p in ResourcePolicy)
        raise ValueError(f"resource_policy must be one of: {allowed}") from exc


def parse_webrtc_ip_handling_policy(value: str | None) -> WebRtcIPHandlingPolicy | None:
    """Parse a Chromium WebRTC IP handling policy; None passes through.

    Raises:
        ValueError: If ``value`` is not a valid policy.
    """
    if value is None:
        return None
    return cast(
        WebRtcIPHandlingPolicy,
        parse_literal(value, WebRtcIPHandlingPolicy, "webrtc_ip_handling_policy"),
    )
