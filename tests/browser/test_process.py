from pathlib import Path

import pytest

from webskrap.browser import process


@pytest.mark.parametrize(
    ("module", "running"),
    [
        ("webskrap.browser.proxy_auth", True),
        ("webskrap.proxy_auth", True),
        ("webskrap.browser.browser_session", False),
    ],
)
def test_proxy_process_check_recognizes_existing_sessions(monkeypatch, module, running):
    monkeypatch.setattr(process.sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_bytes", lambda _: f"python\0-m\0{module}\0".encode())
    assert process.proxy_auth_running(123) is running
