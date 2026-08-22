from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from webskrap.client import WebSkrapError
from webskrap.paths import (
    DEFAULT_OUTPUT_DIRNAME,
    OUTPUT_DIR_ENV,
    output_root,
    resolve_output_path,
    secure_directory,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")


def test_output_root_defaults_under_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(OUTPUT_DIR_ENV, raising=False)
    monkeypatch.chdir(tmp_path)

    assert output_root() == tmp_path / DEFAULT_OUTPUT_DIRNAME


def test_output_root_honors_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(OUTPUT_DIR_ENV, str(tmp_path / "shots"))

    assert output_root() == tmp_path / "shots"


def test_resolve_plain_filename(tmp_path: Path) -> None:
    resolved = resolve_output_path("page.png", root=tmp_path)

    assert resolved == tmp_path / "page.png"


def test_resolve_creates_nested_directories_inside_root(tmp_path: Path) -> None:
    resolved = resolve_output_path("runs/today/page.png", root=tmp_path)

    assert resolved == tmp_path / "runs" / "today" / "page.png"
    assert resolved.parent.is_dir()


def test_resolve_generates_a_name_when_omitted(tmp_path: Path) -> None:
    resolved = resolve_output_path(None, root=tmp_path)

    assert resolved.parent == tmp_path
    assert resolved.name.startswith("webskrap-")
    assert resolved.suffix == ".png"


def test_resolve_uses_output_root_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(OUTPUT_DIR_ENV, str(tmp_path / "shots"))

    assert resolve_output_path("page.png") == tmp_path / "shots" / "page.png"


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("../escape.png", id="parent"),
        pytest.param("../../escape.png", id="grandparent"),
        pytest.param("runs/../../escape.png", id="normalized-traversal"),
        pytest.param("./runs/./../../escape.png", id="dotted-traversal"),
        pytest.param("..", id="bare-parent"),
    ],
)
def test_resolve_rejects_traversal(tmp_path: Path, path: str) -> None:
    root = tmp_path / "out"

    with pytest.raises(WebSkrapError, match="escapes it"):
        resolve_output_path(path, root=root)

    assert not (tmp_path / "escape.png").exists()


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(".", id="dot"),
        pytest.param("", id="empty"),
        pytest.param("./", id="dot-slash"),
    ],
)
def test_resolve_rejects_paths_naming_no_file(tmp_path: Path, path: str) -> None:
    # These resolve to a directory, not a destination file: writing to them
    # would fail inside Playwright rather than at the boundary check.
    with pytest.raises(WebSkrapError, match="does not name a file"):
        resolve_output_path(path, root=tmp_path)


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/etc/webskrap.png", id="absolute"),
        pytest.param("/tmp/shot.png", id="absolute-tmp"),
    ],
)
def test_resolve_rejects_absolute_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(WebSkrapError, match="is absolute"):
        resolve_output_path(path, root=tmp_path)


def test_resolve_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WebSkrapError, match="escapes it"):
        resolve_output_path("link/shot.png", root=root)


def test_resolve_does_not_create_directories_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "out"

    with pytest.raises(WebSkrapError):
        resolve_output_path("../elsewhere/shot.png", root=root)

    assert not (tmp_path / "elsewhere").exists()


def test_resolve_reports_unwritable_directories(tmp_path: Path) -> None:
    root = tmp_path / "out"
    # A file where the output directory must go: mkdir cannot succeed.
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(WebSkrapError, match="could not create output directory"):
        resolve_output_path("runs/shot.png", root=root)


@posix_only
def test_secure_directory_creates_owner_only(tmp_path: Path) -> None:
    created = secure_directory(tmp_path / "root" / "session")

    assert stat.S_IMODE(created.stat().st_mode) == 0o700


@posix_only
def test_secure_directory_tightens_an_existing_directory(tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)

    secure_directory(loose)

    assert stat.S_IMODE(loose.stat().st_mode) == 0o700


@posix_only
def test_secure_directory_leaves_a_shared_directory_alone(tmp_path: Path) -> None:
    # A directory the user set up deliberately (a shared WEBSKRAP_BROWSER_DIR)
    # must not have its permissions narrowed behind their back.
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)

    secure_directory(shared, tighten_existing=False)

    assert stat.S_IMODE(shared.stat().st_mode) == 0o755


@posix_only
def test_secure_directory_still_creates_missing_dirs_owner_only(tmp_path: Path) -> None:
    created = secure_directory(tmp_path / "fresh", tighten_existing=False)

    assert stat.S_IMODE(created.stat().st_mode) == 0o700


@posix_only
def test_secure_directory_does_not_chmod_through_a_symlink(tmp_path: Path) -> None:
    # A symlink planted where a session directory belongs must not hand the
    # mode change to whatever it points at.
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    secure_directory(link)

    assert stat.S_IMODE(target.stat().st_mode) == 0o755


@posix_only
def test_output_root_is_created_owner_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "out"
    monkeypatch.setenv(OUTPUT_DIR_ENV, str(root))

    resolve_output_path("shot.png")

    assert stat.S_IMODE(root.stat().st_mode) == 0o700


@posix_only
def test_existing_output_root_keeps_its_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "out"
    root.mkdir(mode=0o755)
    monkeypatch.setenv(OUTPUT_DIR_ENV, str(root))

    resolve_output_path("shot.png")

    assert stat.S_IMODE(root.stat().st_mode) == 0o755
