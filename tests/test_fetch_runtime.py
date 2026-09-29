from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from anyio import CancelScope, sleep, sleep_forever
from playwright.async_api import BrowserContext

from webskrap import SessionConfig, WebSkrapSession
from webskrap import fetch_runtime as module
from webskrap.errors import WebSkrapError
from webskrap.fetch_runtime import FetchRuntime
from webskrap.profiles import get_profile


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> FetchRuntime:
    runtime = FetchRuntime()
    monkeypatch.setattr(runtime.client, "start", AsyncMock())

    async def create(name, config, profile):
        context = MagicMock(spec=BrowserContext)
        context.close = AsyncMock()
        return WebSkrapSession(name=name, config=config, profile=profile, context=context)

    monkeypatch.setattr(runtime.client, "_create_session", create)
    return runtime


async def test_named_session_reuses_context_and_close_releases_it(runtime: FetchRuntime) -> None:
    async with runtime.use("crawl", SessionConfig(), get_profile(None)) as first:
        pass
    async with runtime.use("crawl", SessionConfig(), get_profile(None)) as second:
        assert first is second
    assert await runtime.close_session("crawl") is True
    assert first._closed
    assert await runtime.close_session("crawl") is False
    async with runtime.use("crawl", SessionConfig(), get_profile(None)) as third:
        assert third is not first
    await runtime.close()
    assert third._closed


async def test_unnamed_calls_are_isolated_and_cleaned_on_failure(runtime: FetchRuntime) -> None:
    async with runtime.use(None, SessionConfig(), get_profile(None)) as first:
        pass
    assert first._closed
    with pytest.raises(RuntimeError, match="navigation failed"):
        async with runtime.use(None, SessionConfig(), get_profile(None)) as second:
            assert second is not first
            raise RuntimeError("navigation failed")
    assert second._closed
    assert runtime.client._sessions == {}
    await runtime.close()


async def test_changed_options_are_rejected_and_busy_close_refused(runtime: FetchRuntime) -> None:
    config = SessionConfig()
    profile = get_profile(None)
    async with runtime.use("crawl", config, profile):
        with pytest.raises(WebSkrapError, match="busy"):
            await runtime.close_session("crawl")
        with pytest.raises(WebSkrapError, match="options changed"):
            async with runtime.use("crawl", SessionConfig(headless=False), profile):
                pytest.fail("changed launch options accepted")
        with pytest.raises(WebSkrapError, match="options changed"):
            async with runtime.use("crawl", config, get_profile("mobile-chrome")):
                pytest.fail("changed profile accepted")
    await runtime.close()


async def test_concurrent_session_launch_is_shared(runtime: FetchRuntime, monkeypatch) -> None:
    create = runtime.client._create_session
    started, release = asyncio.Event(), asyncio.Event()
    launches = 0

    async def blocked_create(*args):
        nonlocal launches
        launches += 1
        started.set()
        await release.wait()
        return await create(*args)

    monkeypatch.setattr(runtime.client, "_create_session", blocked_create)

    async def borrow():
        async with runtime.use("crawl", SessionConfig(), get_profile(None)) as session:
            return session

    first = asyncio.create_task(borrow())
    await started.wait()
    second = asyncio.create_task(borrow())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    session = await second
    assert launches == 1
    await runtime.close()
    assert session._closed


async def test_cancelled_unnamed_launch_is_cleaned(runtime: FetchRuntime, monkeypatch) -> None:
    create = runtime.client._create_session
    started, release = asyncio.Event(), asyncio.Event()
    created = []

    async def blocked_create(*args):
        started.set()
        await release.wait()
        session = await create(*args)
        created.append(session)
        return session

    monkeypatch.setattr(runtime.client, "_create_session", blocked_create)

    async def borrow():
        async with runtime.use(None, SessionConfig(), get_profile(None)):
            pytest.fail("cancelled fetch ran")

    task = asyncio.create_task(borrow())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert created[0]._closed
    assert runtime.client._sessions == {}
    await runtime.close()


async def test_concurrency_limit_and_cancelled_waiter_release_slots(runtime, monkeypatch) -> None:
    monkeypatch.setattr(module, "MAX_FETCH_CONCURRENCY", 2)
    runtime._slots = asyncio.Semaphore(module.MAX_FETCH_CONCURRENCY)
    entered = asyncio.Queue()
    release = asyncio.Event()

    async def fetch_one(index):
        async with runtime.use("crawl", SessionConfig(), get_profile(None)):
            entered.put_nowait(index)
            await release.wait()

    tasks = [asyncio.create_task(fetch_one(i)) for i in range(3)]
    await entered.get()
    await entered.get()
    await asyncio.sleep(0)
    assert entered.empty()
    tasks[-1].cancel()
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(results[-1], asyncio.CancelledError)
    assert runtime._slots._value == 2
    await runtime.close()


async def test_session_limit_recovers_after_close(runtime, monkeypatch) -> None:
    monkeypatch.setattr(module, "MAX_FETCH_SESSIONS", 1)
    async with runtime.use("one", SessionConfig(), get_profile(None)):
        pass
    with pytest.raises(WebSkrapError, match="limit reached"):
        async with runtime.use("two", SessionConfig(), get_profile(None)):
            pytest.fail("session limit exceeded")
    await runtime.close_session("one")
    async with runtime.use("two", SessionConfig(), get_profile(None)):
        pass
    await runtime.close()


async def test_failed_launch_can_retry_with_different_options(runtime, monkeypatch) -> None:
    create = runtime.client._create_session
    monkeypatch.setattr(
        runtime.client, "_create_session", AsyncMock(side_effect=RuntimeError("launch failed"))
    )
    with pytest.raises(RuntimeError, match="launch failed"):
        async with runtime.use("crawl", SessionConfig(), get_profile(None)):
            pytest.fail("failed launch ran")
    monkeypatch.setattr(runtime.client, "_create_session", create)
    async with runtime.use("crawl", SessionConfig(headless=False), get_profile(None)):
        pass
    await runtime.close()


async def test_closed_runtime_rejects_waiting_and_new_calls(runtime) -> None:
    runtime._slots = asyncio.Semaphore(1)
    entered = asyncio.Event()

    async def waiting_call():
        entered.set()
        async with runtime.use("queued", SessionConfig(), get_profile(None)):
            pytest.fail("closed runtime launched a browser")

    async with runtime.use("active", SessionConfig(), get_profile(None)):
        waiter = asyncio.create_task(waiting_call())
        await entered.wait()
        await runtime.close()
    with pytest.raises(WebSkrapError, match="runtime is closed"):
        await waiter
    with pytest.raises(WebSkrapError, match="runtime is closed"):
        async with runtime.use(None, SessionConfig(), get_profile(None)):
            pytest.fail("closed runtime reopened")


async def test_cancelled_named_launch_is_reaped_and_slot_reusable(runtime, monkeypatch) -> None:
    monkeypatch.setattr(module, "MAX_FETCH_SESSIONS", 1)
    create = runtime.client._create_session
    started, release = asyncio.Event(), asyncio.Event()
    created = []
    scopes = asyncio.Queue()

    async def blocked_create(*args):
        started.set()
        await release.wait()
        session = await create(*args)
        created.append(session)
        return session

    monkeypatch.setattr(runtime.client, "_create_session", blocked_create)

    async def borrow():
        with CancelScope() as scope:
            scopes.put_nowait(scope)
            async with runtime.use("cancelled", SessionConfig(), get_profile(None)):
                pytest.fail("cancelled fetch ran")

    task = asyncio.create_task(borrow())
    scope = await scopes.get()
    await started.wait()
    scope.cancel()
    await asyncio.sleep(0)
    release.set()
    await task
    assert created[0]._closed
    assert runtime.client._sessions == {}
    async with runtime.use("new", SessionConfig(), get_profile(None)):
        pass
    await runtime.close()


async def test_repeated_cancellation_releases_named_borrower_under_lock_contention(runtime) -> None:
    scopes = asyncio.Queue()
    entered = asyncio.Event()

    async def borrow():
        with CancelScope() as scope:
            scopes.put_nowait(scope)
            async with runtime.use("crawl", SessionConfig(), get_profile(None)):
                entered.set()
                await sleep_forever()

    task = asyncio.create_task(borrow())
    scope = await scopes.get()
    await entered.wait()
    async with runtime._lock:
        scope.cancel()
        await sleep(0)
        assert not task.done()
    await task
    assert await runtime.close_session("crawl") is True
    await runtime.close()


async def test_closing_browser_does_not_block_other_fetches(runtime, monkeypatch) -> None:
    async with runtime.use("closing", SessionConfig(), get_profile(None)):
        pass
    close = runtime.client.close_session
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked_close(name):
        started.set()
        await release.wait()
        return await close(name)

    monkeypatch.setattr(runtime.client, "close_session", blocked_close)
    closing = asyncio.create_task(runtime.close_session("closing"))
    await started.wait()
    async with runtime.use("other", SessionConfig(), get_profile(None)):
        pass
    with pytest.raises(WebSkrapError, match="is closing"):
        async with runtime.use("closing", SessionConfig(), get_profile(None)):
            pytest.fail("closing name was reused")
    release.set()
    assert await closing
    await runtime.close()


async def test_navigation_timeout_can_vary_without_relaunch(runtime) -> None:
    async with runtime.use("crawl", SessionConfig(), get_profile(None)) as first:
        pass
    async with runtime.use(
        "crawl", SessionConfig(navigation_timeout_ms=120_000), get_profile(None)
    ) as second:
        assert second is first
    await runtime.close()


async def test_cookie_decline_can_vary_without_relaunch(runtime) -> None:
    async with runtime.use("crawl", SessionConfig(), get_profile(None)) as first:
        pass
    async with runtime.use(
        "crawl", SessionConfig(decline_cookies=True), get_profile(None)
    ) as second:
        assert second is first
    await runtime.close()


@pytest.mark.parametrize("name", ["", "  ", "x" * 129])
async def test_invalid_session_name_is_rejected(runtime, name) -> None:
    with pytest.raises(WebSkrapError, match="1-128 characters"):
        async with runtime.use(name, SessionConfig(), get_profile(None)):
            pytest.fail("invalid name accepted")
    await runtime.close()
