"""Chromium launch and fingerprint diagnostics."""

from __future__ import annotations

from typing import Any

from webskrap.browser.executable import find_executable
from webskrap.config.models import SessionConfig
from webskrap.driver import cdp_setup
from webskrap.driver.launch import CDPBrowser, launch
from webskrap.errors import (
    RECOVERY_HINTS,
    ErrorCode,
    is_sandbox_failure,
)

#: Reads the values detectors compare first. Run in an isolated world on a
#: served https page (see :func:`cdp_setup.served_page`), since client hints and
#: media devices exist only in a secure context. Read-only: nothing is patched.
_FINGERPRINT_SCRIPT = """async () => {
  const gl = document.createElement('canvas').getContext('webgl');
  const info = gl && gl.getExtension('WEBGL_debug_renderer_info');
  let architecture = null;
  try {
    architecture = (await navigator.userAgentData.getHighEntropyValues(['architecture']))
      .architecture;
  } catch (error) {}
  let mediaDevices = null;
  try { mediaDevices = (await navigator.mediaDevices.enumerateDevices()).length; } catch (error) {}
  return {
    webdriver: navigator.webdriver,
    user_agent: navigator.userAgent,
    platform: navigator.platform,
    architecture,
    webgl_renderer: gl ? gl.getParameter(info ? info.UNMASKED_RENDERER_WEBGL : gl.RENDERER) : null,
    media_devices: mediaDevices,
  };
}"""
#: A reserved name: :func:`cdp_setup.served_page` answers it, so nothing resolves it.
_FINGERPRINT_URL = "https://webskrap-doctor.invalid/"


async def _read_fingerprint(browser: CDPBrowser) -> dict[str, Any] | None:
    """Return what a default one-shot fetch shows pages, or None if unreadable."""
    try:
        async with cdp_setup.served_page(browser, _FINGERPRINT_URL) as page:
            result = await page.evaluate(_FINGERPRINT_SCRIPT)
    except Exception:  # noqa: BLE001 - a failed probe must not fail the launch report
        return None
    return result if isinstance(result, dict) else None


async def browser_doctor(
    channels: tuple[str | None, ...] = ("chrome", "msedge", "chromium"),
    *,
    chromium_sandbox: bool = True,
) -> dict[str, object]:
    """Report the first Chromium channel that launches headless.

    Also reports the binary it found, since a caller diagnosing a failed
    launch otherwise has to guess where the browser was looked for. The
    browser launches with a default one-shot fetch's flags, and
    ``fingerprint`` reports what pages read from it: ``webdriver``,
    ``user_agent``, ``platform``, client-hint ``architecture``,
    ``webgl_renderer`` and the ``media_devices`` count.
    ``chromium_sandbox`` should match what fetches will use, or a host that
    cannot sandbox reports ready while every fetch fails.
    """
    failure: Exception | None = None
    executable_path: str | None = None
    args = SessionConfig(chromium_sandbox=chromium_sandbox).browser_args()
    for channel in channels:
        try:
            executable = find_executable(channel)
            executable_path = str(executable)
            browser = await launch(executable, args=args, headless=True)
        except Exception as exc:  # noqa: BLE001 - report launch failures
            failure = exc
            if is_sandbox_failure(exc):
                break
            continue
        try:
            version = (await browser.connection.send("Browser.getVersion")).get("product")
            fingerprint = await _read_fingerprint(browser)
        finally:
            await browser.close()
        channel_name = channel or "chromium"
        return {
            "ok": True,
            "message": f"Headless {channel_name} is ready ({version}).",
            "channel": channel_name,
            "browser_identity": {
                "chrome": "Chrome",
                "msedge": "Edge",
                "chromium": "Chromium",
            }.get(channel_name, channel_name),
            "browser_version": version,
            "executable_path": executable_path,
            "fingerprint": fingerprint,
        }
    return {
        "ok": False,
        "message": f"Chromium did not launch: {failure}",
        "channel": None,
        "browser_identity": None,
        "browser_version": None,
        "executable_path": executable_path,
        "fingerprint": None,
        "hint": RECOVERY_HINTS[
            ErrorCode.SANDBOX
            if failure is not None and is_sandbox_failure(failure)
            else ErrorCode.BROWSER_LAUNCH
        ],
    }
