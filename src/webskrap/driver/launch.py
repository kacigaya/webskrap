"""Launch, close, and reap Chromium over CDP."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess  # nosec B404  # noqa: S404 - fixed argv for Chromium, never a shell
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from webskrap.driver.cdp import CDPConnection, CDPPage
from webskrap.driver.transport import (
    MAX_MESSAGE_BYTES,
    PipeTransport,
    WebSocketTransport,
)
from webskrap.errors import SANDBOX_FAILURE_MARKERS, ErrorCode, WebSkrapError

CLOSE_TIMEOUT_S = 5.0
_LOG_TAIL_BYTES = 4096
_POSIX_ONLY = (
    "the debugging pipe needs Linux or macOS (posix_spawn with pipe descriptors); "
    "use transport='port'"
)


@dataclass
class CDPBrowser:
    """A Chromium process WebSkrap launched, on a pipe or a loopback port.

    Closing it asks Chromium to exit, kills it after :data:`CLOSE_TIMEOUT_S`,
    reaps the process, and removes the temporary profile and log it created.
    ``process`` is set for a port launch (:func:`launch` with
    ``transport="port"``); a pipe launch is tracked by ``pid`` alone.
    """

    connection: CDPConnection
    pid: int
    log_path: Path
    temp_dir: Path
    process: subprocess.Popen[bytes] | None = None
    _closed: bool = field(default=False, init=False)

    async def new_page(self, *, context_id: str | None = None) -> CDPPage:
        """Create a blank page target and attach to it with a flat session."""
        params = {"url": "about:blank"}
        if context_id is not None:
            params["browserContextId"] = context_id
        target = await self.connection.send("Target.createTarget", params)
        attached = await self.connection.send(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
        )
        return CDPPage(self.connection, attached["sessionId"], target["targetId"])

    async def close(self) -> None:
        """Stop the browser and clean up. Idempotent."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(WebSkrapError):
            await self.connection.send("Browser.close", timeout_s=CLOSE_TIMEOUT_S)
        await self.connection.close()
        if self.process is not None:
            await asyncio.to_thread(_stop_process, self.process, CLOSE_TIMEOUT_S)
        else:
            await asyncio.to_thread(_reap, self.pid, CLOSE_TIMEOUT_S)
        await asyncio.to_thread(shutil.rmtree, self.temp_dir, True)


def kill_windows_process_tree(pid: int) -> None:
    """Stop a Windows process and its descendants with taskkill."""
    taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe")
    # Fixed argv, no shell; the PID is an int this module recorded.
    subprocess.run(  # nosec B603  # noqa: S603
        [str(taskkill), "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _stop_process(process: subprocess.Popen[bytes], timeout_s: float) -> None:
    """Wait for a port-launched browser to exit, then kill its process tree.

    Chromium's renderers are child processes; killing only the browser
    process would leave them holding the profile. Windows has no process
    groups, so ``taskkill /T`` stops the tree there.
    """
    try:
        process.wait(timeout=timeout_s)
        return
    except subprocess.TimeoutExpired:
        pass
    if sys.platform == "win32":
        kill_windows_process_tree(process.pid)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=timeout_s)


def _reap(pid: int, timeout_s: float) -> None:
    """Wait for ``pid`` to exit, killing its process group after ``timeout_s``."""
    if sys.platform == "win32":
        # launch() already refuses Windows; this guard lets type checkers see it.
        raise WebSkrapError(_POSIX_ONLY, code=ErrorCode.BROWSER_LAUNCH)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            done, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if done:
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)


def _high_fd(fd: int) -> int:
    """Return a close-on-exec copy of ``fd`` numbered 10 or above, closing ``fd``.

    ``posix_spawn`` applies dup2 actions in order. Moving every source descriptor
    out of the 0-4 range first means no action overwrites a later one's source,
    and no source already sits on its target, where dup2 would keep close-on-exec.
    """
    if sys.platform == "win32":
        # launch() already refuses Windows; this guard lets type checkers see it.
        raise WebSkrapError(_POSIX_ONLY, code=ErrorCode.BROWSER_LAUNCH)
    # Imported here: fcntl does not exist on Windows, and importing this module
    # must not break the CLI there.
    import fcntl

    moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(fd)
    return moved


def _log_tail(path: Path) -> str:
    try:
        with path.open("rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - _LOG_TAIL_BYTES))
            return log.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _exit_error(log_path: Path) -> WebSkrapError:
    """Describe a browser that closed its pipe, keeping sandbox text for ``classify``."""
    tail = _log_tail(log_path).lower()
    if any(marker in tail for marker in SANDBOX_FAILURE_MARKERS):
        return WebSkrapError(
            "browser exited during startup: no usable sandbox. Enable unprivileged user "
            "namespaces, or set chromium_sandbox=False to accept weaker isolation",
            code=ErrorCode.SANDBOX,
        )
    return WebSkrapError(
        "browser exited during startup or closed the CDP pipe", code=ErrorCode.BROWSER_LAUNCH
    )


def base_args(user_data_dir: Path) -> list[str]:
    """Return the flags every WebSkrap launch passes after its debugging channel."""
    return [
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # Chrome for Testing is unsigned on macOS, so every launch asks the
        # user for the "Chromium Safe Storage" keychain item. The mock keychain
        # keeps cookie encryption profile-local; pages cannot see it.
        *(["--use-mock-keychain"] if sys.platform == "darwin" else []),
        # Chromium builds (not branded Chrome) apply their testing field
        # trials by default. Stable Chrome never runs that config, and one of
        # its trials trims navigator.languages to a single entry.
        "--disable-field-trial-config",
    ]


async def launch(
    executable: str | Path,
    *,
    args: Iterable[str] = (),
    headless: bool = True,
    user_data_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 30.0,
    transport: Literal["pipe", "port"] | None = None,
) -> CDPBrowser:
    """Start Chromium and wait until it answers over CDP.

    The command line is deliberately short: the debugging channel,
    :func:`base_args`, ``args``, and
    ``--headless`` when requested. Playwright's long default flag list is not
    copied, since each extra flag is a difference from a real Chrome that a
    site might measure.

    Args:
        executable: Chromium or Chrome binary.
        args: Extra Chromium flags, already validated by the caller.
        headless: Add ``--headless`` (Chromium's new headless mode).
        user_data_dir: Persistent profile; a temporary one is used when None.
        env: Browser environment; defaults to this process's.
        timeout_s: How long to wait for the first reply.
        transport: ``pipe`` (the debugging pipe; no port is opened) or
            ``port`` (a loopback debugging port and the WebSocket client).
            None picks ``port`` on Windows, where :func:`os.posix_spawn`
            cannot map the pipe descriptors, and ``pipe`` elsewhere.

    Raises:
        WebSkrapError: ``browser_launch`` or ``sandbox`` when Chromium does not
            start or answer in time.
    """
    if transport is None:
        transport = "port" if sys.platform == "win32" else "pipe"
    executable = Path(executable)
    if not executable.is_file():
        msg = f"Chromium executable is not found at {executable}. Run: webskrap install"
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)

    temp_dir = Path(tempfile.mkdtemp(prefix="webskrap-cdp-"))
    profile = temp_dir / "user-data" if user_data_dir is None else user_data_dir
    profile.mkdir(parents=True, exist_ok=True)
    log_path = temp_dir / "browser.log"
    channel_flag = "--remote-debugging-pipe" if transport == "pipe" else "--remote-debugging-port=0"
    command = [str(executable), channel_flag, *base_args(profile), *args]
    if headless:
        command.append("--headless")
    command.append("about:blank")
    environment = dict(env if env is not None else os.environ)
    if transport == "port":
        browser = await _launch_on_port(
            command, environment, profile, log_path, temp_dir, timeout_s
        )
    else:
        browser = await _launch_on_pipe(command, environment, log_path, temp_dir)
    try:
        await browser.connection.send("Browser.getVersion", timeout_s=timeout_s)
    except BaseException as exc:
        await browser.close()
        if isinstance(exc, WebSkrapError) and exc.code is ErrorCode.TIMEOUT:
            msg = f"browser did not answer over CDP within {timeout_s:.0f}s"
            raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH) from exc
        raise
    return browser


async def _launch_on_pipe(
    command: list[str], env: dict[str, str], log_path: Path, temp_dir: Path
) -> CDPBrowser:
    """Spawn Chromium with fds 3 and 4 mapped to a pipe pair (POSIX only)."""
    if sys.platform == "win32":
        raise WebSkrapError(_POSIX_ONLY, code=ErrorCode.BROWSER_LAUNCH)
    to_browser_read, to_browser_write = os.pipe()
    from_browser_read, from_browser_write = os.pipe()
    child_in = _high_fd(to_browser_read)
    child_out = _high_fd(from_browser_write)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    devnull = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
    try:
        # Fixed argv, no shell; dup2 clears close-on-exec on targets 0-4 only,
        # so the browser inherits nothing else from this process.
        pid = os.posix_spawn(
            command[0],
            command,
            env,
            file_actions=[
                (os.POSIX_SPAWN_DUP2, devnull, 0),
                (os.POSIX_SPAWN_DUP2, log_fd, 1),
                (os.POSIX_SPAWN_DUP2, log_fd, 2),
                (os.POSIX_SPAWN_DUP2, child_in, 3),
                (os.POSIX_SPAWN_DUP2, child_out, 4),
            ],
            setpgroup=0,
        )
    except BaseException:
        for fd in (to_browser_write, from_browser_read):
            os.close(fd)
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        for fd in (child_in, child_out, log_fd, devnull):
            os.close(fd)

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
    read_file = os.fdopen(from_browser_read, "rb", 0)
    write_file = os.fdopen(to_browser_write, "wb", 0)
    try:
        read_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), read_file
        )
        write_transport, _protocol = await loop.connect_write_pipe(asyncio.Protocol, write_file)
    except BaseException:
        # No connection exists yet, so close() cannot ask Chromium to exit.
        read_file.close()
        write_file.close()
        await asyncio.to_thread(_reap, pid, 0)
        await asyncio.to_thread(shutil.rmtree, temp_dir, True)
        raise
    connection = CDPConnection(PipeTransport(reader, write_transport, read_transport))
    connection.on_disconnect = lambda: _exit_error(log_path)
    connection.start()
    return CDPBrowser(connection, pid, log_path, temp_dir)


async def _launch_on_port(
    command: list[str],
    env: dict[str, str],
    profile: Path,
    log_path: Path,
    temp_dir: Path,
    timeout_s: float,
) -> CDPBrowser:
    """Start Chromium on a loopback debugging port and attach over WebSocket.

    Chromium picks a free port and writes it, with the browser target's
    random path, to ``DevToolsActivePort`` in the profile. The port listens
    on 127.0.0.1 only, and only for as long as the browser runs.
    """
    port_file = profile / "DevToolsActivePort"
    port_file.unlink(missing_ok=True)
    # Its own process group (Windows) or session (POSIX): a Ctrl+C in this
    # console does not reach the browser, and close() can stop the whole tree.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    with log_path.open("wb") as log:
        # Fixed argv, no shell; the executable comes from find_executable or
        # the caller, and every flag is a constant or a validated launch arg.
        process = subprocess.Popen(  # nosec B603  # noqa: S603
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            creationflags=flags,
            start_new_session=sys.platform != "win32",
        )
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            if process.poll() is not None:
                raise _exit_error(log_path)
            try:
                lines = port_file.read_text(encoding="utf-8").split()
            except OSError:
                lines = []
            if len(lines) >= 2 and lines[0].isdigit():
                break
            if time.monotonic() >= deadline:
                msg = f"browser did not report a DevTools port within {timeout_s:.0f}s"
                raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
            await asyncio.sleep(0.05)
        transport = await WebSocketTransport.connect(
            "127.0.0.1", int(lines[0]), lines[1], timeout_s=timeout_s
        )
    except BaseException:
        await asyncio.to_thread(_stop_process, process, 0)
        await asyncio.to_thread(shutil.rmtree, temp_dir, True)
        raise
    connection = CDPConnection(transport)
    connection.on_disconnect = lambda: _exit_error(log_path)
    connection.start()
    return CDPBrowser(connection, process.pid, log_path, temp_dir, process)
