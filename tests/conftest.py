from __future__ import annotations

import asyncio
import os
import shutil
import ssl
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from webskrap.browser import browser_session, channels
from webskrap.browser.executable import find_executable
from webskrap.challenges import xvfb_available
from webskrap.driver.cdp import CDPPage
from webskrap.errors import WebSkrapError


def _sandbox_launches() -> bool:
    """True when a sandboxed Chromium actually starts on this host.

    Unprivileged containers and CI images with user namespaces disabled cannot
    run Chromium's sandbox. That is a property of the host, not of the code
    under test, so the session lifecycle tests below fall back instead of
    failing. The sandbox *policy* (which flags a launch gets) is asserted
    without a browser in ``test_browser_session_unit.py``.

    It probes the downloaded Chromium that ``channel="chromium"`` tests pin.
    An installed Chrome, which sessions prefer, may sandbox where it cannot.
    """
    directory = Path(tempfile.mkdtemp(prefix="webskrap-sandbox-probe-"))
    try:
        executable = str(find_executable("chromium"))
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
    channels._resolved_channels.clear()
    channels._resolution_locks.clear()


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


def tls_context(directory: Path, host: str) -> ssl.SSLContext:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "widget.pem", directory / "widget.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    return context


def browser_modes() -> list[Any]:
    has_desktop = sys.platform in ("darwin", "win32") or bool(os.environ.get("DISPLAY"))
    return [
        pytest.param({}, id="headless"),
        pytest.param(
            {"virtual_display": True},
            id="virtual-display",
            marks=pytest.mark.skipif(not xvfb_available(), reason="virtual_display needs Xvfb"),
        ),
        pytest.param(
            {"headless": False},
            id="headed",
            marks=pytest.mark.skipif(not has_desktop, reason="headed needs a desktop"),
        ),
    ]
