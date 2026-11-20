"""Fallback between Chromium browser channels."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from webskrap.config.models import SessionConfig
from webskrap.errors import (
    is_launch_failure,
    is_sandbox_failure,
)

T = TypeVar("T")

# Channel that actually launched for each requested one, including the
# requested channel itself when it works. Remembered for the process so later
# calls skip the failed launch, and so a warm MCP fetch session keeps the same
# config on every call instead of tripping its changed-options check.
_resolved_channels: dict[str | None, str | None] = {}
# Serializes exclusive calls for a requested channel until its channel is
# known, so concurrent first calls on one named fetch session never try
# different channels against the same session entry. Deliberate limit: those
# cold-start calls wait for the first one to finish, once per process.
_resolution_locks: dict[str | None, asyncio.Lock] = {}


def _fallback_channels(channel: str | None) -> tuple[str | None, ...]:
    if channel in (None, "chromium"):
        return (channel,)
    if channel == "chrome":
        return (channel, "msedge", "chromium")
    return (channel, "chromium")


async def with_channel_fallback(
    run: Callable[[SessionConfig], Awaitable[T]],
    config: SessionConfig,
    *,
    on_retry: Callable[[str | None, str | None], None] | None = None,
    exclusive: bool = False,
) -> T:
    """Run ``run(config)``, trying Edge and bundled Chromium when Chrome cannot launch.

    The default channel is ``chrome``, which does not exist on every platform
    (Linux ARM64 has no Chrome build). An installed Edge is tried before the
    bundled Chromium. Sandbox failures and non-launch errors are raised at
    once; when every channel fails to launch, the last failure is raised.

    Args:
        run: Launches a browser with the given config and does the work.
        config: Requested config; only ``channel`` changes between attempts.
        on_retry: Called with the failed and next channel before each retry.
        exclusive: Resolve the channel one call at a time. Needed when calls
            share state keyed by config, such as a named warm fetch session.
    """
    requested = config.channel
    if len(_fallback_channels(requested)) == 1:
        return await run(config)
    if exclusive and requested not in _resolved_channels:
        async with _resolution_locks.setdefault(requested, asyncio.Lock()):
            if requested not in _resolved_channels:
                return await _run_channels(run, config, on_retry)
    return await _run_channels(run, config, on_retry)


async def _run_channels(
    run: Callable[[SessionConfig], Awaitable[T]],
    config: SessionConfig,
    on_retry: Callable[[str | None, str | None], None] | None,
) -> T:
    requested = config.channel
    channels = _fallback_channels(requested)
    if requested in _resolved_channels:
        channels = channels[channels.index(_resolved_channels[requested]) :]
    for index, channel in enumerate(channels):
        selected = (
            config if channel == requested else config.model_copy(update={"channel": channel})
        )
        try:
            result = await run(selected)
        except Exception as exc:
            if is_sandbox_failure(exc):
                raise
            if not is_launch_failure(exc):
                # The browser started; the page or navigation failed.
                _resolved_channels[requested] = channel
                raise
            if index + 1 == len(channels):
                raise
            if on_retry is not None:
                on_retry(channel, channels[index + 1])
            continue
        _resolved_channels[requested] = channel
        return result
    raise RuntimeError("no browser channel to launch")
