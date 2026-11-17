from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from webskrap.display import DIRECTORY_PREFIX, _helper
from webskrap.errors import ErrorCode, WebSkrapError

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="signals a POSIX helper")

_FAKE_HELPER = """
import sys
from contextlib import contextmanager
from pathlib import Path
from webskrap.display import _helper

@contextmanager
def hold(width, height):
    Path(sys.argv[1], "held").write_text(f"{width}x{height}")
    try:
        yield 100, 200
    finally:
        Path(sys.argv[1], "released").write_text("yes")

sys.exit(_helper.run(Path(sys.argv[1]), 640, 480, detached=sys.argv[2] == "1", backend=hold))
"""


def _directory(tmp_path: Path) -> Path:
    directory = tmp_path / f"{DIRECTORY_PREFIX}test"
    directory.mkdir()
    (directory / _helper.RUN_FILE).touch()
    return directory


def _spawn(directory: Path, *, detached: bool) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _FAKE_HELPER, str(directory), "1" if detached else "0"],
        stdin=subprocess.DEVNULL if detached else subprocess.PIPE,
    )


def _wait_ready(directory: Path) -> dict[str, int | str]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ready = directory / _helper.READY_FILE
        if ready.exists():
            return json.loads(ready.read_text())
        time.sleep(0.02)
    raise AssertionError("helper never became ready")


def test_run_holds_until_the_run_file_goes(tmp_path: Path) -> None:
    directory = _directory(tmp_path)
    events: list[str] = []

    @contextmanager
    def hold(width: int, height: int) -> Iterator[tuple[int, int]]:
        events.append(f"enter {width}x{height}")
        yield 1440, 0
        events.append("exit")

    status: list[int] = []
    thread = threading.Thread(
        target=lambda: status.append(_helper.run(directory, 800, 600, detached=True, backend=hold))
    )
    thread.start()
    try:
        ready = _wait_ready(directory)
        assert (ready["x"], ready["y"]) == (1440, 0)
        assert _helper._lock_held(directory / _helper.LOCK_FILE)
    finally:
        (directory / _helper.RUN_FILE).unlink(missing_ok=True)
        thread.join(5)

    assert status == [0]
    assert events == ["enter 800x600", "exit"]
    assert not _helper._lock_held(directory / _helper.LOCK_FILE)


def test_run_reports_a_backend_failure(tmp_path: Path) -> None:
    directory = _directory(tmp_path)

    @contextmanager
    def hold(_width: int, _height: int) -> Iterator[tuple[int, int]]:
        raise WebSkrapError("no driver installed", code=ErrorCode.BROWSER_LAUNCH)
        yield 0, 0

    assert _helper.run(directory, 800, 600, detached=True, backend=hold) == 1
    assert (directory / _helper.ERROR_FILE).read_text() == "no driver installed"
    assert not (directory / _helper.READY_FILE).exists()


@pytest.mark.skipif(sys.platform in ("darwin", "win32"), reason="a real backend exists there")
def test_start_reports_the_helper_error_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Path] = []
    real_mkdtemp = _helper.tempfile.mkdtemp

    def tracking_mkdtemp(**kwargs: str) -> str:
        created.append(Path(real_mkdtemp(**kwargs)))
        return str(created[-1])

    monkeypatch.setattr(_helper.tempfile, "mkdtemp", tracking_mkdtemp)

    with pytest.raises(WebSkrapError, match="macOS and Windows only") as excinfo:
        _helper.start(800, 600, detached=False)

    assert excinfo.value.code is ErrorCode.BROWSER_LAUNCH
    assert not created[0].exists()


def test_stop_detached_releases_a_detached_helper(tmp_path: Path) -> None:
    directory = _directory(tmp_path)
    process = _spawn(directory, detached=True)
    try:
        ready = _wait_ready(directory)
        assert (ready["x"], ready["y"]) == (100, 200)

        _helper.stop_detached(process.pid, directory)

        assert process.wait(5) == 0
        assert not directory.exists()
    finally:
        process.kill()


def test_owner_exit_releases_an_attached_helper(tmp_path: Path) -> None:
    directory = _directory(tmp_path)
    process = _spawn(directory, detached=False)
    try:
        _wait_ready(directory)
        assert process.stdin is not None
        process.stdin.close()

        assert process.wait(5) == 0
        assert (directory / "released").read_text() == "yes"
    finally:
        process.kill()


def test_helper_main_refuses_a_foreign_directory(tmp_path: Path) -> None:
    assert _helper.main([str(tmp_path), "800", "600"]) == 2


@posix_only
def test_stop_detached_signals_a_stuck_helper_only_while_it_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_helper, "STOP_TIMEOUT_S", 0.3)
    directory = _directory(tmp_path)
    lock = directory / _helper.LOCK_FILE
    lock.touch()
    stuck = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import fcntl, os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "fd = os.open(sys.argv[1], os.O_RDWR)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "time.sleep(60)\n",
            str(lock),
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert stuck.stdout is not None
        assert stuck.stdout.readline().strip() == b"locked"

        started = time.monotonic()
        _helper.stop_detached(stuck.pid, directory)
        stuck.wait(5)

        assert time.monotonic() - started < 10
        assert not directory.exists()
    finally:
        stuck.kill()


def test_stop_detached_never_signals_without_the_lock(tmp_path: Path) -> None:
    directory = _directory(tmp_path)
    bystander = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    try:
        _helper.stop_detached(bystander.pid, directory)

        assert bystander.poll() is None
        assert not directory.exists()
    finally:
        bystander.kill()
        bystander.wait(5)


@posix_only
def test_process_identity_rejects_a_live_unrelated_pid(tmp_path: Path) -> None:
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    try:
        assert _helper._is_helper(process.pid, _directory(tmp_path)) is False
    finally:
        process.kill()
        process.wait(5)
