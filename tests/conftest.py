from __future__ import annotations

import asyncio
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from webskrap import browser_session, client
from webskrap.cdp import CDPPage
from webskrap.client import WebSkrapError


def _sandbox_launches() -> bool:
    """True when a sandboxed Chromium actually starts on this host.

    Unprivileged containers and CI images with user namespaces disabled cannot
    run Chromium's sandbox. That is a property of the host, not of the code
    under test, so the session lifecycle tests below fall back instead of
    failing. The sandbox *policy* (which flags a launch gets) is asserted
    without a browser in ``test_browser_session_unit.py``.
    """
    directory = Path(tempfile.mkdtemp(prefix="webskrap-sandbox-probe-"))
    try:
        executable = asyncio.run(browser_session.chromium_executable())
        pid, _port = browser_session.launch_browser(
            directory, executable=executable, headless=True, chromium_sandbox=True
        )
    except WebSkrapError:
        return False
    else:
        browser_session._terminate(
            directory,
            {"pid": pid, "created_at": datetime.now(UTC).isoformat(timespec="seconds")},
        )
        return True
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="session")
def sandbox_supported() -> bool:
    """Probe Chromium sandbox support once per test session."""
    return _sandbox_launches()


@pytest.fixture
def persistent_session_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sandbox_supported: bool,
) -> Path:
    """Point persistent sessions at ``tmp_path``, sandboxed where possible."""
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path))
    if sandbox_supported:
        monkeypatch.delenv(browser_session.SANDBOX_ENV, raising=False)
    else:
        monkeypatch.setenv(browser_session.SANDBOX_ENV, "0")
    return tmp_path


@pytest.fixture(autouse=True)
def _forget_resolved_channels() -> None:
    # The channel fallback memo is process-wide; one test's fallback must not
    # skip another test's first launch attempt.
    client._resolved_channels.clear()
    client._resolution_locks.clear()


async def set_page_content(page: CDPPage, html: str, *, block_requests: bool = True) -> None:
    """Replace ``page``'s document with ``html``, as Playwright's set_content did.

    With ``block_requests``, every request the content would make fails, so no
    third-party traffic leaves a test.
    """
    if block_requests:
        tasks: set[asyncio.Task[Any]] = set()

        def fail(params: dict[str, Any]) -> None:
            task = asyncio.create_task(
                page.send(
                    "Fetch.failRequest",
                    {"requestId": params["requestId"], "errorReason": "BlockedByClient"},
                )
            )
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.connection.on("Fetch.requestPaused", fail, session_id=page.session_id)
        await page.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
    await page.send("Page.setDocumentContent", {"frameId": page.target_id, "html": html})
