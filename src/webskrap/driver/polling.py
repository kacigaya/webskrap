"""Deadline and polling helpers for CDP page operations."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TypeVar

from webskrap.driver.cdp import CDPError
from webskrap.errors import ErrorCode, WebSkrapError

_POLL_S = 0.1
T = TypeVar("T")


async def _within(awaitable: Awaitable[T], deadline: float) -> T:
    """Await ``awaitable`` but give up at ``deadline``, raising ``TimeoutError``.

    A busy renderer can stall one evaluation for its whole command timeout;
    bounding each step keeps a wait inside the time the caller asked for.
    """
    return await asyncio.wait_for(awaitable, max(0.0, deadline - time.monotonic()))


async def poll(check: Callable[[], Awaitable[bool]], timeout_ms: float, what: str) -> None:
    """Poll the async predicate ``check`` until it is true or time runs out."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        # A navigation can destroy the isolated world mid-call; poll again.
        with suppress(CDPError, TimeoutError):
            if await _within(check(), deadline):
                return
        if time.monotonic() >= deadline:
            msg = f"timed out after {timeout_ms:.0f}ms waiting for {what}"
            raise WebSkrapError(msg, code=ErrorCode.TIMEOUT)
        await asyncio.sleep(_POLL_S)
