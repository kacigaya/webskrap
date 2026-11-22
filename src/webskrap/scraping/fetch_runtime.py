"""Bounded, server-owned fetch sessions with explicit state sharing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from anyio import CancelScope
from anyio.lowlevel import checkpoint_if_cancelled

from webskrap.config.models import SessionConfig
from webskrap.config.profile import BrowserProfile
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.scraping.client import WebSkrapClient
from webskrap.scraping.session import WebSkrapSession

MAX_FETCH_CONCURRENCY = 8
MAX_FETCH_SESSIONS = 8
_PER_CALL = {
    "navigation_timeout_ms",
    "decline_cookies",
    "solve_cloudflare",
    "solve_datadome",
    "solve_perimeterx",
    "solve_recaptcha",
}


@dataclass
class _Entry:
    config: SessionConfig
    profile: BrowserProfile
    active: int = 0
    session: WebSkrapSession | None = None
    closing: bool = False


class FetchRuntime:
    """Share Chromium across overlapping fetches with compatible settings.

    Only named sessions share a browser context and persist after their last
    fetch. At most eight fetches run and eight named sessions stay open.
    """

    def __init__(self) -> None:
        """Set limits without launching a driver or browser."""
        self.client = WebSkrapClient()
        self._slots = asyncio.Semaphore(MAX_FETCH_CONCURRENCY)
        self._lock = asyncio.Lock()
        self._entries: dict[str, _Entry] = {}
        self._transient: dict[str, _Entry] = {}
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
                async with self._use_unnamed(config, profile) as session:
                    yield session
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
                    entry.config.model_dump(exclude=_PER_CALL)
                    != config.model_dump(exclude=_PER_CALL)
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

    @asynccontextmanager
    async def _use_unnamed(
        self, config: SessionConfig, profile: BrowserProfile
    ) -> AsyncIterator[WebSkrapSession]:
        if (
            not config.launch_headless
            or config.user_data_dir is not None
            or config.storage_state is not None
        ):
            async with self.client._temporary_session(
                "fetch", config=config, profile=profile
            ) as session:
                yield session
            return

        options = config.model_dump(exclude=_PER_CALL)
        async with self._lock:
            if self._closed:
                raise WebSkrapError("fetch runtime is closed", code=ErrorCode.USAGE)
            for candidate, entry in self._transient.items():
                if (
                    not entry.closing
                    and entry.profile == profile
                    and entry.config.model_dump(exclude=_PER_CALL) == options
                ):
                    name = candidate
                    break
            else:
                name = f"_fetch_{uuid4().hex}"
                entry = _Entry(config.model_copy(deep=True), profile.model_copy(deep=True))
                self._transient[name] = entry
            entry.active += 1

        try:
            owner = await self.client.session(name, config=entry.config, profile=entry.profile)
            cancelled = None
            creation = asyncio.create_task(
                owner.browser.connection.send(
                    "Target.createBrowserContext", {"disposeOnDetach": True}
                )
            )
            try:
                with CancelScope(shield=True):
                    while not creation.done():
                        try:
                            await asyncio.wait((creation,))
                        except asyncio.CancelledError as exc:
                            cancelled = exc
                context = creation.result()
            except BaseException:
                entry.closing = True
                if cancelled is not None:
                    raise cancelled from None
                raise
            session = WebSkrapSession(
                name=name,
                browser=owner.browser,
                config=config,
                profile=profile,
                context_id=context["browserContextId"],
            )
            try:
                if cancelled is not None:
                    raise cancelled
                await checkpoint_if_cancelled()
                yield session
            finally:
                try:
                    await session.close()
                except BaseException as exc:
                    entry.closing = True
                    if not isinstance(exc, WebSkrapError):
                        raise
        finally:
            with CancelScope(shield=True):
                async with self._lock:
                    entry.active -= 1
                    idle = entry.active == 0
                    if idle:
                        self._transient.pop(name)
                if idle:
                    await self.client.close_session(name)

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
