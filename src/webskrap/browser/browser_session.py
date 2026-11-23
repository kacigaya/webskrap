"""Persistent interactive browser sessions shared by the CLI and MCP server.

``open_session`` launches a detached Chromium that outlives the calling
process; every action reconnects to it over CDP with WebSkrap's own client
(:mod:`webskrap.driver.cdp_session`), acts on the current page, and disconnects. The
browser gets the same launch flags as a one-shot fetch, and no action sends
the CDP ``Runtime.enable`` command that detectors look for. Snapshots carry
``eN`` refs that interaction helpers accept alongside CSS selectors.

All failures raise :class:`~webskrap.errors.WebSkrapError`; presentation
layers translate them for their medium.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import shutil
import subprocess  # nosec B404  # noqa: S404 - fixed argv for Chromium, never a shell
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from anyio import CancelScope

from webskrap.browser import display
from webskrap.browser.actions import (
    DEFAULT_ACTION_TIMEOUT_MS,
    goto,
)
from webskrap.browser.executable import find_executable
from webskrap.browser.gpu import effective_gpu, require_lavapipe
from webskrap.browser.process import (
    _PROCESS_QUERY_LIMITED_INFORMATION as _PROCESS_QUERY_LIMITED_INFORMATION,
)
from webskrap.browser.process import _STILL_ACTIVE as _STILL_ACTIVE
from webskrap.browser.process import LAUNCH_TIMEOUT_S as LAUNCH_TIMEOUT_S
from webskrap.browser.process import PROXY_AUTH_MODULE as PROXY_AUTH_MODULE
from webskrap.browser.process import _ps_command as _ps_command
from webskrap.browser.process import _windows_process_start as _windows_process_start
from webskrap.browser.process import _windows_session_running as _windows_session_running
from webskrap.browser.process import kill_browser as kill_browser
from webskrap.browser.process import proxy_auth_running as proxy_auth_running
from webskrap.browser.process import session_running as session_running
from webskrap.browser.session_state import SESSION_NAME_PATTERN as SESSION_NAME_PATTERN
from webskrap.browser.session_state import (
    _acquire_session_operation_lock as _acquire_session_operation_lock,
)
from webskrap.browser.session_state import _SessionOperationLock as _SessionOperationLock
from webskrap.browser.session_state import create_session_dir as create_session_dir
from webskrap.browser.session_state import is_session_dir as is_session_dir
from webskrap.browser.session_state import read_state as read_state
from webskrap.browser.session_state import session_dir as session_dir
from webskrap.browser.session_state import sessions_root as sessions_root
from webskrap.browser.session_state import state_path as state_path
from webskrap.browser.session_state import write_state as write_state
from webskrap.config.models import (
    GpuBackend,
    SessionConfig,
    SessionProfileMode,
    WebRtcIPHandlingPolicy,
)
from webskrap.config.profile import BrowserProfile, ProxyConfig
from webskrap.config.profiles import get_profile
from webskrap.driver import targets
from webskrap.driver.cdp import CDPPage
from webskrap.driver.launch import base_args
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.security.paths import secure_directory

logger = logging.getLogger("webskrap.browser_session")

T = TypeVar("T")

#: The page type session actions receive; see :func:`run_page_action`.
SessionPage = CDPPage

DEVTOOLS_PORT_FILE = "DevToolsActivePort"
CLOSE_TIMEOUT_S = 5.0
DEFAULT_NAVIGATION_TIMEOUT_MS = 30_000.0
SANDBOX_ENV = "WEBSKRAP_CHROMIUM_SANDBOX"
#: Where ``webskrap browser open --proxy`` reads proxy credentials. They are
#: removed from the environment of the browser and the helper it starts.
PROXY_USERNAME_ENV = "WEBSKRAP_PROXY_USERNAME"
PROXY_PASSWORD_ENV = "WEBSKRAP_PROXY_PASSWORD"  # nosec B105  # noqa: S105 - a variable name
# Chromium logs one of these when its setuid/namespace sandbox cannot start
# (unprivileged user namespaces off, or running as root in a container).
# Matched against the browser log, unlike errors.SANDBOX_FAILURE_MARKERS.
_LOG_SANDBOX_MARKERS = (
    "no usable sandbox",
    "sandboxhelper",
    "setuid sandbox",
    "clone_newuser",
    "--no-sandbox",
)

#: Actions :func:`tabs` accepts.
TAB_ACTIONS = ("list", "new", "select", "close")


def sandbox_enabled(chromium_sandbox: bool | None = None) -> bool:
    """Resolve whether a session's Chromium keeps its OS sandbox.

    An explicit ``chromium_sandbox`` argument wins. Otherwise the
    ``WEBSKRAP_CHROMIUM_SANDBOX`` environment variable decides, so deployments
    that cannot sandbox (unprivileged containers) can opt out for callers such
    as the MCP server, which deliberately does not expose the switch to models.
    Sandboxing is on unless something explicitly turns it off.
    """
    if chromium_sandbox is not None:
        return chromium_sandbox
    value = os.environ.get(SANDBOX_ENV)
    if value is None:
        return True
    return value.strip().lower() not in ("0", "false", "no", "off")


async def chromium_executable() -> str:
    """Return the binary sessions launch: installed Chrome, then Edge, then Chromium.

    Same order as one-shot fetches. Installed Chrome and Edge ship the
    platform's sandbox support (for example an AppArmor ``userns`` profile on
    Ubuntu 23.10+), which a downloaded Chrome for Testing lacks. See
    :func:`webskrap.browser.executable.find_executable`.

    Raises:
        WebSkrapError: ``browser_launch`` when no Chromium is installed.
    """
    for channel in ("chrome", "msedge"):
        with suppress(WebSkrapError):
            return str(find_executable(channel))
    return str(find_executable("chromium"))


def _session_config(
    *,
    headless: bool,
    chromium_sandbox: bool,
    proxy_server: str | None,
    webrtc_ip_handling_policy: WebRtcIPHandlingPolicy | None,
    fake_media_devices: bool,
    virtual_display: bool,
    gpu: GpuBackend,
    profile_mode: SessionProfileMode,
    profile: str | None,
    user_data_dir: Path | None = None,
) -> tuple[SessionConfig, BrowserProfile | None]:
    """Return a detached session's config and the profile its launch applies.

    Built from :class:`~webskrap.config.models.SessionConfig`, so a session gets the
    flags a one-shot fetch would: ``--disable-blink-features=AutomationControlled``
    (otherwise a remote-debugging port sets ``navigator.webdriver``), the
    headless virtual screen, ``--no-sandbox`` only when the sandbox is off,
    the proxy and its WebRTC default, the GL flags of ``gpu``, and the
    profile's ``--accept-lang`` in locale mode.

    Raises:
        WebSkrapError: ``usage`` for an invalid proxy, an unknown profile, or
            locale mode off Linux, where only CDP emulation applies a locale.
    """
    proxy = ProxyConfig(server=persistent_proxy_server(proxy_server)) if proxy_server else None
    config = SessionConfig(
        headless=headless,
        chromium_sandbox=chromium_sandbox,
        proxy=proxy,
        webrtc_ip_handling_policy=webrtc_ip_handling_policy,
        fake_media_devices=fake_media_devices,
        virtual_display=virtual_display,
        gpu=gpu,
        profile_mode=profile_mode,
        user_data_dir=user_data_dir,
    )
    if profile_mode == "native":
        return config, None
    if not config.uses_native_profile():
        msg = "profile_mode='locale' for a browser session needs Linux"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    try:
        return config, get_profile(profile)
    except ValueError as exc:
        raise WebSkrapError(str(exc), code=ErrorCode.USAGE) from exc


def persistent_proxy_server(server: str) -> str:
    """Validate a proxy URL for a persistent session and return it.

    Credentials in the URL are refused: Chromium ignores them in
    ``--proxy-server``, and other local users can read a command line. Pass
    them to :func:`open_session` as ``proxy_username`` and ``proxy_password``.

    Raises:
        WebSkrapError: If ``server`` has no http/https/socks4/socks5 scheme or
            embeds credentials (``usage``).
    """
    if "@" in server.split("://", 1)[-1].split("/", 1)[0]:
        msg = (
            "proxy URL must not embed user:password@: set "
            f"{PROXY_USERNAME_ENV} and {PROXY_PASSWORD_ENV} instead"
        )
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    try:
        ProxyConfig(server=server)
    except ValueError as exc:
        msg = "proxy server must start with http://, https://, socks4://, or socks5://"
        raise WebSkrapError(msg, code=ErrorCode.USAGE) from exc
    return server


def launch_browser(
    directory: Path,
    *,
    executable: str,
    headless: bool,
    chromium_sandbox: bool = True,
    proxy_server: str | None = None,
    webrtc_ip_handling_policy: WebRtcIPHandlingPolicy | None = None,
    fake_media_devices: bool = False,
    virtual_display: bool = False,
    gpu: GpuBackend = GpuBackend.AUTO,
    profile_mode: SessionProfileMode = "native",
    profile: str | None = None,
    display_env: dict[str, str] | None = None,
) -> tuple[int, int]:
    """Start a detached Chromium and return its (pid, CDP port).

    Args:
        directory: Session directory; the profile lives in ``user-data`` below it.
        executable: Chromium binary to run.
        headless: Launch without a visible window.
        chromium_sandbox: Keep Chromium's OS sandbox. Passing False adds
            ``--no-sandbox``, which lets a compromised renderer processing a
            hostile page reach the rest of the machine. Only do that where the
            sandbox genuinely cannot start.
        proxy_server: Unauthenticated proxy URL for all browser traffic.
        webrtc_ip_handling_policy: Chromium WebRTC ICE policy; defaults to
            ``disable_non_proxied_udp`` when ``proxy_server`` is set.
        fake_media_devices: Supply a synthetic camera and microphone.
        virtual_display: Run headed on the display in ``display_env`` when
            ``headless`` is set; the caller starts that display.
        gpu: WebGL renderer; ``mesa`` renders through Mesa's lavapipe.
        profile_mode: ``locale`` sets ``profile``'s timezone and languages
            through the browser's environment and ``--accept-lang`` (Linux).
        profile: Bundled profile name for locale mode.
        display_env: ``DISPLAY`` and ``XAUTHORITY`` of a virtual display.

    Raises:
        WebSkrapError: If the browser exits during startup or never reports a
            DevTools port.
    """
    config, native_profile = _session_config(
        headless=headless,
        chromium_sandbox=chromium_sandbox,
        proxy_server=proxy_server,
        webrtc_ip_handling_policy=webrtc_ip_handling_policy,
        fake_media_devices=fake_media_devices,
        virtual_display=virtual_display,
        gpu=gpu,
        profile_mode=profile_mode,
        profile=profile,
        user_data_dir=directory / "user-data",
    )
    # The locale environment is built from os.environ, credentials included.
    env = _child_env({**(config.browser_env(native_profile) or os.environ), **(display_env or {})})
    user_data_dir = secure_directory(directory / "user-data")
    port_file = user_data_dir / DEVTOOLS_PORT_FILE
    port_file.unlink(missing_ok=True)
    log_path = directory / "browser.log"

    command = [
        executable,
        "--remote-debugging-port=0",
        *base_args(user_data_dir),
        # A bfcache restore re-fires no load events, stranding the
        # go_back/go_forward load wait. Each command attaches anew and cannot
        # track restores across attachments, so trade bfcache for
        # deterministic load events.
        "--disable-features=BackForwardCache",
        *config.browser_args(native_profile),
    ]
    if config.launch_headless:
        command.append("--headless=new")
    command.append("about:blank")

    with log_path.open("wb") as log:
        # Fixed argv, no shell. The executable comes from find_executable (the
        # installed Chrome for Testing), and every argument is either a
        # constant flag or this session's own validated directory -- nothing
        # here is reachable from page content.
        process = subprocess.Popen(  # nosec B603  # noqa: S603
            command,
            stdout=log,
            stderr=log,
            env=env,
            start_new_session=True,
        )
    try:
        port = _wait_for_startup(
            process, log_path, lambda: _devtools_port(port_file), "report a DevTools port"
        )
        # Chromium can report its DevTools port before it creates the startup
        # tab, most often on Windows. An attach in that gap finds no page and
        # opens a second one, and later commands may then pick the blank tab.
        _wait_for_startup(
            process,
            log_path,
            lambda: True if _has_page_target(port) else None,
            "open its startup page",
        )
        return process.pid, port
    except BaseException:
        # Ctrl-C or a startup failure must not orphan a detached browser that
        # holds the profile lock with no state file pointing at it.
        kill_browser(process.pid, force=True)
        raise


def _child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return ``base`` (this process's environment) without the proxy credentials."""
    return {
        key: value
        for key, value in (os.environ if base is None else base).items()
        if key not in {PROXY_USERNAME_ENV, PROXY_PASSWORD_ENV}
    }


async def start_proxy_auth(directory: Path, port: int, credentials: tuple[str, str]) -> int:
    """Start the helper that answers the session proxy's challenges; return its PID.

    The helper (:mod:`webskrap.browser.proxy_auth`) gets the credentials on stdin and
    keeps them in memory only. It stays connected to the browser and exits
    when the browser does.

    Raises:
        WebSkrapError: ``browser_launch`` if the helper exits or is not ready
            within ``LAUNCH_TIMEOUT_S``.
    """
    log_path = directory / "proxy-auth.log"
    with log_path.open("wb") as log:
        # Fixed argv, no shell: this interpreter, a module of this package,
        # the browser's port and the session's own validated directory.
        process = subprocess.Popen(  # nosec B603  # noqa: S603
            [sys.executable, "-m", PROXY_AUTH_MODULE, str(port), str(directory / "user-data")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            env=_child_env(),
            start_new_session=True,
        )
    stdin, stdout = process.stdin, process.stdout
    try:
        if stdin is None or stdout is None:  # pragma: no cover - PIPE sets both
            raise WebSkrapError("proxy authentication helper has no pipes")
        username, password = credentials
        stdin.write(json.dumps({"username": username, "password": password}).encode() + b"\n")
        stdin.close()
        try:
            line = await asyncio.wait_for(asyncio.to_thread(stdout.readline), LAUNCH_TIMEOUT_S)
        except TimeoutError:
            line = b""
        if line.strip() != b"ready":
            msg = f"proxy authentication helper did not start; see {log_path}"
            raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
    except BaseException:
        process.kill()
        process.wait(timeout=CLOSE_TIMEOUT_S)
        raise
    finally:
        if stdout is not None:
            stdout.close()
    return process.pid


def _sandbox_hint(log_path: Path) -> str:
    """Return advice about the sandbox when the browser log blames it.

    Never retries without the sandbox: dropping it silently would turn a
    configuration problem into a permanent, invisible loss of isolation.
    """
    try:
        log = log_path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return ""
    if not any(marker in log for marker in _LOG_SANDBOX_MARKERS):
        return ""
    return (
        ". Chromium's sandbox could not start. Enable unprivileged user "
        "namespaces, or accept the weaker isolation with "
        f"'webskrap browser open --no-sandbox' (or {SANDBOX_ENV}=0)"
    )


def _wait_for_startup(
    process: subprocess.Popen[bytes], log_path: Path, check: Callable[[], T | None], what: str
) -> T:
    """Poll ``check`` until it returns a value, failing if the browser exits."""
    deadline = time.monotonic() + LAUNCH_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            msg = f"browser exited during startup; see {log_path}{_sandbox_hint(log_path)}"
            # No explicit code: a sandbox failure appends its own hint to this
            # message, and `classify` reads that to report SANDBOX instead.
            raise WebSkrapError(msg)
        if (value := check()) is not None:
            return value
        time.sleep(0.05)
    msg = f"browser did not {what} within {LAUNCH_TIMEOUT_S:.0f}s"
    raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)


def _devtools_port(port_file: Path) -> int | None:
    try:
        first_line = port_file.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    return int(first_line) if first_line.isdigit() else None


def _has_page_target(port: int) -> bool:
    """True when the DevTools endpoint on ``port`` lists at least one page."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/json/list")
        response = connection.getresponse()
        if response.status != 200:
            return False
        targets = json.loads(response.read())
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        connection.close()
    return isinstance(targets, list) and any(
        isinstance(target, dict) and target.get("type") == "page" for target in targets
    )


async def open_session(
    name: str,
    *,
    headless: bool = True,
    chromium_sandbox: bool | None = None,
    proxy_server: str | None = None,
    webrtc_ip_handling_policy: WebRtcIPHandlingPolicy | None = None,
    fake_media_devices: bool = False,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
    virtual_display: bool = False,
    gpu: GpuBackend = GpuBackend.AUTO,
    profile_mode: SessionProfileMode = "native",
    profile: str | None = None,
) -> dict[str, Any]:
    """Start (or reuse) a persistent browser session.

    The session directory is created owner-only; it accumulates cookies and
    logged-in state that outlive the calling process.

    Args:
        name: Session name; letters, digits, ``.``, ``_`` or ``-``.
        headless: Launch without a visible window.
        chromium_sandbox: Keep Chromium's OS sandbox. None consults
            ``WEBSKRAP_CHROMIUM_SANDBOX`` and otherwise sandboxes. See
            :func:`sandbox_enabled` and :func:`launch_browser`.
        proxy_server: Proxy URL for all of the session's traffic, without
            credentials; see :func:`persistent_proxy_server`.
        webrtc_ip_handling_policy: Chromium WebRTC ICE policy. Defaults to
            ``disable_non_proxied_udp`` when ``proxy_server`` is set.
        fake_media_devices: Supply a synthetic camera and microphone.
        proxy_username: User name for an http or https ``proxy_server``.
        proxy_password: Password for an http or https ``proxy_server``. With
            either one set, a helper process (:func:`start_proxy_auth`) keeps
            both in memory and answers the proxy for as long as the browser
            runs. Neither is written to disk.
        virtual_display: With ``headless``, run headed on a private Xvfb
            display that lives as long as the session (Linux). Pages then see
            no ``HeadlessChrome`` token and get full client hints.
        gpu: WebGL renderer; ``mesa`` uses Mesa's lavapipe instead of
            SwiftShader (Linux with mesa-vulkan-drivers).
        profile_mode: ``locale`` launches with ``profile``'s timezone and
            languages (Linux). ``full`` emulation is not offered: it ends when
            the CDP connection that set it closes.
        profile: Bundled profile for locale mode; ``desktop-chrome`` when None.

    Returns:
        ``{"session", "pid", "port", "reused", "chromium_sandbox",
        "proxy_server", "proxy_auth", "webrtc_ip_handling_policy",
        "fake_media_devices", "virtual_display", "gpu", "profile_mode",
        "profile"}``. Every key after ``reused`` describes the running
        browser's launch settings.

    Raises:
        WebSkrapError: If the name, proxy or profile is invalid, the browser,
            display or proxy helper fails to start, or a running session was
            launched with a different proxy, proxy authentication, WebRTC
            policy or requested stealth setting (``usage``). Launch flags
            cannot change on a running browser, and silently reusing it would
            send traffic outside the requested proxy.
    """
    credentials = None
    if proxy_username is not None or proxy_password is not None:
        credentials = (proxy_username or "", proxy_password or "")
    if proxy_server is not None:
        persistent_proxy_server(proxy_server)
    if credentials is not None and not (proxy_server or "").startswith(("http://", "https://")):
        # Chromium answers no challenge from a SOCKS proxy.
        msg = "proxy credentials need an http:// or https:// proxy server"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    config, _ = _session_config(
        headless=headless,
        chromium_sandbox=True,
        proxy_server=proxy_server,
        webrtc_ip_handling_policy=webrtc_ip_handling_policy,
        fake_media_devices=fake_media_devices,
        virtual_display=virtual_display,
        gpu=gpu,
        profile_mode=profile_mode,
        profile=profile,
    )
    launch = {
        "virtual_display": config.uses_virtual_display(),
        "gpu": str(gpu),
        "profile_mode": profile_mode,
        "profile": (profile or "desktop-chrome") if profile_mode == "locale" else None,
    }
    operation_lock = await _acquire_session_operation_lock(name)
    try:
        # Unconditional, so a session directory created by an older version (or
        # under a loose umask) is tightened on the next open, not only on relaunch.
        directory = create_session_dir(name)
        existing = read_state(directory)
        state = existing if session_running(directory, existing) else None
        reused = state is not None
        if state is not None:
            _require_same_network(name, state, proxy_server, webrtc_ip_handling_policy)
            if credentials is not None and not state.get("proxy_auth_pid"):
                msg = (
                    f"session '{name}' is already running without proxy authentication. "
                    f"Run: webskrap browser close --session {name}, then open it again"
                )
                raise WebSkrapError(msg, code=ErrorCode.USAGE)
            if fake_media_devices and not state.get("fake_media_devices", False):
                msg = (
                    f"session '{name}' is already running without fake media devices. "
                    f"Run: webskrap browser close --session {name}, then open it again"
                )
                raise WebSkrapError(msg, code=ErrorCode.USAGE)
            _require_same_launch(name, state, launch, explicit_profile=profile is not None)

        if state is None:
            if existing is not None:
                # A crashed browser leaves its display running.
                _stop_display(existing)
            launch_gpu = effective_gpu(gpu, virtual_display=config.uses_virtual_display())
            if launch_gpu is GpuBackend.MESA:
                require_lavapipe()
            executable = await chromium_executable()
            sandboxed = sandbox_enabled(chromium_sandbox)
            virtual = None
            if config.uses_virtual_display():
                screen = config.virtual_screen()
                virtual = await display.VirtualDisplay.start(screen.width, screen.height)
            display_env = virtual.env if virtual is not None else None
            try:
                pid, port = await _launch_off_loop(
                    lambda: launch_browser(
                        directory,
                        executable=executable,
                        headless=headless,
                        chromium_sandbox=sandboxed,
                        proxy_server=proxy_server,
                        webrtc_ip_handling_policy=webrtc_ip_handling_policy,
                        fake_media_devices=fake_media_devices,
                        virtual_display=virtual_display,
                        gpu=launch_gpu,
                        profile_mode=profile_mode,
                        profile=profile,
                        display_env=display_env,
                    )
                )
            except BaseException:
                if virtual is not None:
                    with CancelScope(shield=True):
                        await virtual.stop()
                raise
            display_state = (
                {"display_pid": virtual.pid, "display_dir": str(virtual.directory)}
                if virtual is not None
                else {}
            )
            helper = None
            if credentials is not None:
                try:
                    helper = await start_proxy_auth(directory, port, credentials)
                except BaseException:
                    kill_browser(pid, force=True)
                    _stop_display(display_state)
                    raise
            state = {
                "pid": pid,
                "port": port,
                "headless": headless,
                # Recorded so `browser list` can show which running sessions gave
                # up renderer isolation; a reused session keeps its launch value.
                "chromium_sandbox": sandboxed,
                # Recorded so a later open can refuse to reuse this browser for
                # a different proxy. Never holds credentials; those are refused.
                "proxy_server": proxy_server,
                # The helper that answers the proxy; it holds the credentials.
                "proxy_auth_pid": helper,
                "fake_media_devices": fake_media_devices,
                "webrtc_ip_handling_policy": SessionConfig(
                    proxy=ProxyConfig(server=proxy_server) if proxy_server else None,
                    webrtc_ip_handling_policy=webrtc_ip_handling_policy,
                ).effective_webrtc_ip_handling_policy(),
                **launch,
                "gpu": str(launch_gpu),
                **display_state,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            try:
                write_state(directory, state)
            except BaseException:
                # Without a state file the running browser would be unreachable
                # and would hold the profile lock against the next open.
                kill_browser(pid, force=True)
                _stop_display(display_state)
                raise

        return {
            "session": name,
            "pid": state["pid"],
            "port": state["port"],
            "reused": reused,
            "chromium_sandbox": bool(state.get("chromium_sandbox", False)),
            "proxy_server": state.get("proxy_server"),
            "proxy_auth": bool(state.get("proxy_auth_pid")),
            "webrtc_ip_handling_policy": state.get("webrtc_ip_handling_policy"),
            "fake_media_devices": bool(state.get("fake_media_devices", False)),
            **{key: state.get(key, _LAUNCH_DEFAULTS[key]) for key in _LAUNCH_DEFAULTS},
        }
    finally:
        await asyncio.to_thread(operation_lock.release)


async def _launch_off_loop(start: Callable[[], tuple[int, int]]) -> tuple[int, int]:
    """Run ``start`` (a :func:`launch_browser` call) in a worker thread.

    The launch polls with blocking sleeps for up to ``LAUNCH_TIMEOUT_S``; in
    the thread, other tasks on the event loop keep running. A cancelled caller
    cannot stop the thread, so the browser it starts is killed instead of
    being left running with no state file.
    """
    launch = asyncio.ensure_future(asyncio.to_thread(start))
    try:
        return await asyncio.shield(launch)
    except asyncio.CancelledError:
        # An anyio scope cancels every later await too; shield the wait.
        with CancelScope(shield=True), suppress(Exception):
            pid, _port = await launch
            kill_browser(pid, force=True)
        raise


#: Stealth launch settings a session records, with the value of a session
#: started before they were recorded.
_LAUNCH_DEFAULTS: dict[str, Any] = {
    "virtual_display": False,
    "gpu": str(GpuBackend.AUTO),
    "profile_mode": "native",
    "profile": None,
}


def _require_same_launch(
    name: str, state: dict[str, Any], launch: dict[str, Any], *, explicit_profile: bool
) -> None:
    """Refuse to reuse a running session for a stealth setting it lacks.

    As with the network settings, only what the caller asked for counts: a
    default value, or a profile left unnamed, reuses the session as it runs.
    """
    asked = {key: value != _LAUNCH_DEFAULTS[key] for key, value in launch.items()}
    asked["profile"] = explicit_profile and launch["profile_mode"] == "locale"
    mismatched = [
        key
        for key, value in launch.items()
        if asked[key] and state.get(key, _LAUNCH_DEFAULTS[key]) != value
    ]
    if mismatched:
        msg = (
            f"session '{name}' is already running with a different "
            f"{', '.join(mismatched)}. Run: webskrap browser close --session {name}, "
            "then open it again"
        )
        raise WebSkrapError(msg, code=ErrorCode.USAGE)


def _stop_display(state: dict[str, Any]) -> None:
    """Stop the virtual display a session state records, if any."""
    pid, directory = state.get("display_pid"), state.get("display_dir")
    if isinstance(pid, int) and isinstance(directory, str):
        display.stop_detached(pid, Path(directory))


def _require_same_network(
    name: str,
    state: dict[str, Any],
    proxy_server: str | None,
    webrtc_ip_handling_policy: WebRtcIPHandlingPolicy | None,
) -> None:
    """Refuse to reuse a running session for a different proxy or WebRTC policy.

    Only settings the caller asked for are compared: reopening a proxied
    session without naming the proxy reuses it, and its traffic still goes
    through that proxy.
    """
    mismatched = [
        label
        for label, requested, running in (
            ("proxy", proxy_server, state.get("proxy_server")),
            (
                "WebRTC policy",
                webrtc_ip_handling_policy,
                state.get("webrtc_ip_handling_policy"),
            ),
        )
        if requested is not None and requested != running
    ]
    if mismatched:
        msg = (
            f"session '{name}' is already running with a different "
            f"{' and '.join(mismatched)}. Run: webskrap browser close --session {name}, "
            "then open it again"
        )
        raise WebSkrapError(msg, code=ErrorCode.USAGE)


def close_session(name: str, *, delete_data: bool = False) -> dict[str, Any]:
    """Stop a session's browser; profile data persists unless ``delete_data``."""
    operation_lock = _SessionOperationLock.acquire(name)
    try:
        directory = session_dir(name)
        state = read_state(directory)
        if state is not None and session_running(directory, state):
            _terminate(directory, state)
        if state is not None:
            _stop_display(state)
        state_path(directory).unlink(missing_ok=True)
        if delete_data:
            try:
                shutil.rmtree(directory, ignore_errors=False)
            except OSError:
                logger.debug("could not remove session directory %s", directory)
    finally:
        operation_lock.release()
    return {"session": name, "deleted_data": delete_data}


def _terminate(directory: Path, state: dict[str, Any]) -> None:
    pid = state["pid"]
    kill_browser(pid, force=False)
    deadline = time.monotonic() + CLOSE_TIMEOUT_S
    while time.monotonic() < deadline:
        # The full state keeps the recycled-PID check active while polling.
        if not session_running(directory, state):
            return
        time.sleep(0.05)
    kill_browser(pid, force=True)


def list_session_names() -> list[str]:
    """Return the names of every on-disk session, sorted."""
    root = sessions_root()
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if is_session_dir(path))


def list_sessions() -> list[dict[str, Any]]:
    """Return one entry per session.

    Each is ``{"session", "running", "pid", "port", "chromium_sandbox",
    "proxy_server", "proxy_auth"}``. The last five are None for a session
    that is not running. ``chromium_sandbox`` and ``proxy_server`` report how the running
    browser was launched, so an operator can see which sessions gave up
    renderer isolation and where each one's traffic goes.
    """
    sessions = []
    for name in list_session_names():
        directory = sessions_root() / name
        state = read_state(directory)
        live = state if state is not None and session_running(directory, state) else None
        sessions.append(
            {
                "session": name,
                "running": live is not None,
                "pid": live and live["pid"],
                "port": live and live["port"],
                "chromium_sandbox": live and bool(live.get("chromium_sandbox", False)),
                "proxy_server": live and live.get("proxy_server"),
                "proxy_auth": live and bool(live.get("proxy_auth_pid")),
            }
        )
    return sessions


def _open_state(name: str) -> tuple[Path, dict[str, Any]]:
    """Return an open session's directory and state, or raise why it cannot act."""
    directory = session_dir(name)
    state = read_state(directory)
    if state is None:
        msg = f"session '{name}' is not open. Run: webskrap browser open"
        raise WebSkrapError(msg, code=ErrorCode.NO_SESSION)
    helper = state.get("proxy_auth_pid")
    if helper and not proxy_auth_running(helper):
        # Without it every request would fail at the proxy with a 407.
        msg = (
            f"session '{name}' lost its proxy authentication helper. "
            "Run: webskrap browser close, then webskrap browser open"
        )
        raise WebSkrapError(msg, code=ErrorCode.SESSION_UNREACHABLE)
    return directory, state


async def run_page_action(
    name: str,
    action: Callable[[SessionPage], Awaitable[T]],
    *,
    timeout_ms: float = DEFAULT_ACTION_TIMEOUT_MS,
) -> T:
    """Connect to the session's browser, run ``action`` on the current page.

    Attaches over WebSkrap's own WebSocket client (:func:`targets.attach`)
    to the current tab (:func:`targets.current_page`), as each CLI
    command and MCP call is a new connection. Sessions opened by WebSkrap
    2.x, with either driver, attach the same way.
    """
    directory, state = _open_state(name)
    async with targets.attach(
        name, directory / "user-data", state["port"], timeout_ms=timeout_ms
    ) as page:
        return await action(page)


async def tabs(
    name: str,
    action: str,
    *,
    index: int | None = None,
    url: str | None = None,
    timeout_ms: float = DEFAULT_NAVIGATION_TIMEOUT_MS,
) -> dict[str, Any]:
    """List, open, select or close the session's tabs.

    ``action`` is ``list``, ``new`` (then ``url`` is loaded in the new tab),
    ``select`` (needs ``index``) or ``close`` (``index``, or the current tab).
    Indexes count from 0 in the order the tabs opened. Later commands act on
    the current tab.

    Returns:
        ``{"tabs": [{"index", "url", "title", "current"}]}``; ``new`` with a
        ``url`` adds the load's ``status``, ``url`` and ``title``.

    Raises:
        WebSkrapError: ``usage`` for an unknown action, a missing or unknown
            index, or a ``url`` outside ``new``.
    """
    if action not in TAB_ACTIONS:
        msg = f"unknown tab action '{action}': use one of {', '.join(TAB_ACTIONS)}"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    if action == "select" and index is None:
        raise WebSkrapError("'select' needs a tab index", code=ErrorCode.USAGE)
    if action in {"list", "new"} and index is not None:
        raise WebSkrapError(f"'{action}' takes no tab index", code=ErrorCode.USAGE)
    if url is not None and action != "new":
        raise WebSkrapError("only 'new' takes a url", code=ErrorCode.USAGE)
    directory, state = _open_state(name)
    user_data = directory / "user-data"
    async with targets.connect(name, user_data, state["port"], timeout_ms=timeout_ms) as connection:
        if action == "list":
            listing = await targets.list_tabs(connection, directory)
        elif action == "new":
            listing = await targets.new_tab(connection, directory)
        elif action == "select" and index is not None:
            listing = await targets.select_tab(connection, directory, index)
        else:
            listing = await targets.close_tab(connection, directory, index)
    if url is None:
        return {"tabs": listing}
    loaded = await run_page_action(
        name, lambda page: goto(page, url, "load"), timeout_ms=timeout_ms
    )
    async with targets.connect(name, user_data, state["port"], timeout_ms=timeout_ms) as connection:
        return {"tabs": await targets.list_tabs(connection, directory), **loaded}
