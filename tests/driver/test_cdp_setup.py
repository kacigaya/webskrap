"""Failure handling for browser-level profile setup."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from webskrap.config.profiles import get_profile
from webskrap.driver import cdp_setup
from webskrap.driver.cdp import CDPConnection, CDPError, CDPPage


async def test_desktop_window_sizing_allows_an_older_browser_without_the_command() -> None:
    connection = MagicMock(spec=CDPConnection)
    connection.send = AsyncMock(
        side_effect=[{"windowId": 7}, CDPError("CDP error -32601: method not found")]
    )
    page = CDPPage(connection, "session", "page")
    await cdp_setup._fit_window(page, get_profile(None))


@pytest.mark.parametrize(
    "error",
    [CDPError("CDP error -32000: browser window not found"), asyncio.CancelledError()],
)
async def test_desktop_window_sizing_preserves_real_failures_and_cancellation(error) -> None:
    connection = MagicMock(spec=CDPConnection)
    connection.send = AsyncMock(side_effect=[{"windowId": 7}, error])
    page = CDPPage(connection, "session", "page")
    with pytest.raises(type(error)):
        await cdp_setup._fit_window(page, get_profile(None))
