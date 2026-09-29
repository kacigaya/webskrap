"""Bounded, server-owned fetch sessions with explicit state sharing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from anyio import CancelScope

from webskrap.client import WebSkrapClient, WebSkrapSession
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.models import BrowserProfile, SessionConfig

MAX_FETCH_CONCURRENCY = 8
MAX_FETCH_SESSIONS = 8


@dataclass
class _Entry:
    config: SessionConfig
    profile: BrowserProfile
    active: int = 0
    session: WebSkrapSession | None = None
    closing: bool = False


class FetchRuntime:
    """Keep the driver warm and reuse only explicitly named browser contexts.

    Owned by the MCP lifespan. Calls without a name still get a fresh browser
    and profile. At most eight fetches run and eight named sessions stay open.
    """

    def __init__(self) -> None:
        """Set limits without launching a driver or browser."""
        self.client = WebSkrapClient()
        self._slots = asyncio.Semaphore(MAX_FETCH_CONCURRENCY)
        self._lock = asyncio.Lock()
        self._entries: dict[str, _Entry] = {}
        self._closed = False

    @asynccontextmanager
    async def use(
        self, name: str | None, config: SessionConfig, profile: BrowserProfile
    ) -> AsyncIterator[WebSkrapSession]:
        """Borrow a context; reject changed options on a live named session."""
        if name is not None and (not name.strip() or len(name) > 128):
            raise WebSkrapError("session must contain 1-128 characters", code=ErrorCode.USAGE)
        async with self._slots:
            if self._closed:
                raise WebSkrapError("fetch runtime is closed", code=ErrorCode.USAGE)
            if name is None:
                # The client owns cleanup even if launch is cancelled.
                transient_name = f"_fetch_{uuid4().hex}"
                try:
                    session = await self.client.session(
                        transient_name, config=config, profile=profile
                    )
                    yield session
                finally:
                    with CancelScope(shield=True):
                        await self.client.close_session(transient_name)
                return
            async with self._lock:
                entry = self._entries.get(name)
                if entry is None:
                    if len(self._entries) >= MAX_FETCH_SESSIONS:
                        raise WebSkrapError(
                            "fetch session limit reached; use fetch_session_close first",
                            code=ErrorCode.USAGE,
                        )
                    # No caller mutation can change the reuse contract.
                    entry = _Entry(config.model_copy(deep=True), profile.model_copy(deep=True))
                    self._entries[name] = entry
                elif entry.closing:
                    raise WebSkrapError("fetch session is closing", code=ErrorCode.USAGE)
                elif (
                    entry.config.model_dump(exclude={"navigation_timeout_ms", "decline_cookies"})
                    != config.model_dump(exclude={"navigation_timeout_ms", "decline_cookies"})
                    or entry.profile != profile
                ):
                    raise WebSkrapError(
                        "fetch session options changed; use fetch_session_close first",
                        code=ErrorCode.USAGE,
                    )
                entry.active += 1
            try:
                entry.session = await self.client.session(
                    f"_mcp_{name}", config=entry.config, profile=entry.profile
                )
                yield entry.session
            finally:
                with CancelScope(shield=True):
                    async with self._lock:
                        entry.active -= 1
                        abandoned_launch = entry.session is None and entry.active == 0
                        if abandoned_launch:
                            entry.closing = True
                    if abandoned_launch:
                        await self._close_entry(name, entry)

    async def close_session(self, name: str) -> bool:
        """Close an idle fetch session; refuse to interrupt active callers."""
        async with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                return False
            if entry.active:
                raise WebSkrapError("fetch session is busy", code=ErrorCode.USAGE)
            if entry.closing:
                raise WebSkrapError("fetch session is closing", code=ErrorCode.USAGE)
            entry.closing = True
        await self._close_entry(name, entry)
        return True

    async def _close_entry(self, name: str, entry: _Entry) -> None:
        # Keep the name reserved, without blocking unrelated session operations.
        with CancelScope(shield=True):
            try:
                await self.client.close_session(f"_mcp_{name}")
            except BaseException:
                async with self._lock:
                    entry.closing = False
                raise
            else:
                async with self._lock:
                    self._entries.pop(name, None)

    async def close(self) -> None:
        """Release all browsers and the driver when the server stops."""
        self._closed = True
        with CancelScope(shield=True):
            try:
                await self.client.close()
            finally:
                self._entries.clear()
