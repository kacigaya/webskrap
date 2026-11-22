"""Bounded, server-owned fetch sessions with explicit state sharing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
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
class _Pool:
    config: SessionConfig
    profile: BrowserProfile
    active: int = 0
    closing: bool = False


@dataclass
class _NamedEntry:
    config: SessionConfig
    profile: BrowserProfile
    opening: asyncio.Task[tuple[WebSkrapSession, AsyncExitStack | None]]
    active: int = 0
    used: bool = False
    closing: asyncio.Task[None] | None = None


def _can_pool(config: SessionConfig) -> bool:
    return config.launch_headless and config.user_data_dir is None and config.storage_state is None


class FetchRuntime:
    """Share Chromium across compatible fetches, with a separate context per name.

    Only named sessions share a browser context and persist after their last
    fetch. At most eight fetches run and eight named sessions stay open.
    """

    def __init__(self) -> None:
        """Set limits without launching a driver or browser."""
        self.client = WebSkrapClient()
        self._slots = asyncio.Semaphore(MAX_FETCH_CONCURRENCY)
        self._lock = asyncio.Lock()
        self._entries: dict[str, _NamedEntry] = {}
        self._named: dict[str, _Pool] = {}
        self._transient: dict[str, _Pool] = {}
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
                if self._closed:
                    raise WebSkrapError("fetch runtime is closed", code=ErrorCode.USAGE)
                entry = self._entries.get(name)
                if entry is None:
                    if len(self._entries) >= MAX_FETCH_SESSIONS:
                        raise WebSkrapError(
                            "fetch session limit reached; use fetch_session_close first",
                            code=ErrorCode.USAGE,
                        )
                    saved_config = config.model_copy(deep=True)
                    saved_profile = profile.model_copy(deep=True)
                    entry = _NamedEntry(
                        saved_config,
                        saved_profile,
                        asyncio.create_task(self._open_named(name, saved_config, saved_profile)),
                    )
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
                session, _ = await asyncio.shield(entry.opening)
                if self._closed:
                    raise WebSkrapError("fetch runtime is closed", code=ErrorCode.USAGE)
                entry.used = True
                yield session
            finally:
                with CancelScope(shield=True):
                    async with self._lock:
                        entry.active -= 1
                        closing = None
                        if not entry.used and entry.active == 0 and not self._closed:
                            closing = self._start_close(name, entry)
                    if closing is not None:
                        await asyncio.shield(closing)

    async def _open_named(
        self, name: str, config: SessionConfig, profile: BrowserProfile
    ) -> tuple[WebSkrapSession, AsyncExitStack | None]:
        if not _can_pool(config):
            return await self.client.session(f"_mcp_{name}", config=config, profile=profile), None
        async with AsyncExitStack() as stack:
            session = await stack.enter_async_context(
                self._use_pooled(config, profile, self._named)
            )
            return session, stack.pop_all()

    @asynccontextmanager
    async def _use_unnamed(
        self, config: SessionConfig, profile: BrowserProfile
    ) -> AsyncIterator[WebSkrapSession]:
        if not _can_pool(config):
            async with self.client._temporary_session(
                "fetch", config=config, profile=profile
            ) as session:
                yield session
            return

        async with self._use_pooled(config, profile, self._transient) as session:
            yield session

    @asynccontextmanager
    async def _use_pooled(
        self, config: SessionConfig, profile: BrowserProfile, pools: dict[str, _Pool]
    ) -> AsyncIterator[WebSkrapSession]:
        options = config.model_dump(exclude=_PER_CALL)
        async with self._lock:
            if self._closed:
                raise WebSkrapError("fetch runtime is closed", code=ErrorCode.USAGE)
            for candidate, entry in pools.items():
                if (
                    not entry.closing
                    and entry.profile == profile
                    and entry.config.model_dump(exclude=_PER_CALL) == options
                ):
                    name = candidate
                    break
            else:
                name = f"_fetch_{uuid4().hex}"
                entry = _Pool(config.model_copy(deep=True), profile.model_copy(deep=True))
                pools[name] = entry
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
                context_id = creation.result()["browserContextId"]
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
                context_id=context_id,
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
                        pools.pop(name)
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
            closing = self._start_close(name, entry)
        with CancelScope(shield=True):
            await asyncio.shield(closing)
        return True

    def _start_close(self, name: str, entry: _NamedEntry) -> asyncio.Task[None]:
        if entry.closing is None:
            entry.closing = asyncio.create_task(self._close_entry(name, entry))
        return entry.closing

    async def _close_entry(self, name: str, entry: _NamedEntry) -> None:
        # Keep the name reserved, without blocking unrelated session operations.
        with CancelScope(shield=True):
            try:
                results = await asyncio.gather(entry.opening, return_exceptions=True)
                opened = results[0]
                if not _can_pool(entry.config):
                    await self.client.close_session(f"_mcp_{name}")
                elif not isinstance(opened, BaseException) and opened[1] is not None:
                    await opened[1].aclose()
            finally:
                async with self._lock:
                    self._entries.pop(name, None)

    async def close(self) -> None:
        """Release all browsers and the driver when the server stops."""
        self._closed = True
        with CancelScope(shield=True):
            try:
                async with self._lock:
                    closing = [
                        self._start_close(name, entry) for name, entry in self._entries.items()
                    ]
                results = await asyncio.gather(*closing, return_exceptions=True)
                if error := next(
                    (result for result in results if isinstance(result, BaseException)), None
                ):
                    raise error
            finally:
                try:
                    await self.client.close()
                finally:
                    self._entries.clear()
