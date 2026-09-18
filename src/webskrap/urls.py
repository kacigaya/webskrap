"""URL validation for fetch and navigation targets.

Two trust levels share this module:

* :func:`validate_url` is the core check applied everywhere, including the
  trusted Python API: the scheme must be ``http``/``https``, a host must be
  present, and embedded credentials are rejected so they never reach logs or
  state files.
* :func:`validate_mcp_url` adds the model-boundary check: the host must not
  resolve to a non-public address (loopback, RFC1918, link-local, ...), unless
  ``WEBSKRAP_ALLOW_PRIVATE_NET`` opts in. The Python API and CLI take their
  URLs from the operator, so they skip the DNS lookup; the MCP server takes
  them from a model that reads untrusted pages.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit

from webskrap.errors import ErrorCode, WebSkrapError

PRIVATE_NET_ENV = "WEBSKRAP_ALLOW_PRIVATE_NET"


def private_net_allowed() -> bool:
    """Return True when private-network targets are explicitly opted in."""
    return os.environ.get(PRIVATE_NET_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def validate_url(url: str) -> str:
    """Check ``url`` is a fetchable URL without embedded credentials.

    ``http``/``https`` targets must carry a host and no ``user:pass@``
    authority. ``data:`` URLs and ``about:blank`` perform no network request
    to an attacker-chosen host, so they are allowed (tests and local page
    scaffolding use them); everything else non-web (``file:``, ``ftp:``,
    ``javascript:``) is rejected.

    Args:
        url: Candidate target, possibly with surrounding whitespace.

    Returns:
        The stripped URL.

    Raises:
        WebSkrapError: If the scheme is not fetchable, no host is present,
            or the authority carries ``user:pass@`` credentials.
    """
    candidate = url.strip()
    try:
        parsed = urlsplit(candidate)
    except ValueError as exc:
        msg = f"invalid URL '{candidate}': {exc}"
        raise WebSkrapError(msg, code=ErrorCode.USAGE) from exc
    scheme = parsed.scheme.lower()
    if scheme == "data" or candidate == "about:blank":
        return candidate
    if scheme not in ("http", "https"):
        msg = (
            f"invalid URL '{candidate}': scheme must be http or https. "
            "Only web pages can be fetched."
        )
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    if not parsed.hostname:
        msg = f"invalid URL '{candidate}': no host to fetch."
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    if parsed.username or parsed.password or "@" in parsed.netloc:
        msg = (
            f"invalid URL '{candidate}': remove credentials from the URL. "
            "Pass proxy credentials via ProxyConfig instead."
        )
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    return candidate


def _host_is_blocked_literal(hostname: str) -> bool:
    """Return True when ``hostname`` is literally non-public, without DNS."""
    lowered = hostname.strip().lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(lowered).is_global
    except ValueError:
        return False


def _resolve_blocked(hostname: str) -> bool:
    """Return True when ``hostname`` resolves to any non-public address."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        # Unresolvable here: leave it to the browser, which reports navigation.
        return False
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False
    for raw in addresses:
        try:
            if not ipaddress.ip_address(raw).is_global:
                return True
        except ValueError:
            return True
    return False


async def reject_private_network(url: str) -> None:
    """Reject ``url`` when its host is non-public and no opt-in is set.

    Args:
        url: Already scheme-validated URL.

    Raises:
        WebSkrapError: If the host is non-public and ``WEBSKRAP_ALLOW_PRIVATE_NET``
            is not set to a truthy value.
    """
    if private_net_allowed():
        return
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        # data: and about:blank perform no network request; nothing to guard.
        return
    hostname = urlsplit(url).hostname or ""
    if _host_is_blocked_literal(hostname):
        blocked = True
    else:
        blocked = await asyncio.to_thread(_resolve_blocked, hostname)
    if blocked:
        msg = (
            f"invalid URL '{url}': private or local hosts are blocked. "
            f"Set {PRIVATE_NET_ENV}=1 to allow them."
        )
        raise WebSkrapError(msg, code=ErrorCode.USAGE)


async def validate_mcp_url(url: str) -> str:
    """Validate a model-supplied URL: scheme check plus private-net guard.

    Args:
        url: Candidate target from MCP tool input.

    Returns:
        The stripped URL.

    Raises:
        WebSkrapError: If :func:`validate_url` or :func:`reject_private_network`
            rejects it.
    """
    candidate = validate_url(url)
    await reject_private_network(candidate)
    return candidate
