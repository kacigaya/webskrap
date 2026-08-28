"""Filesystem policy for paths WebSkrap does not fully control.

Two concerns live here:

* **MCP path confinement.** The MCP server takes paths from a model, which in
  turn reads untrusted pages. :func:`resolve_output_path` and
  :func:`resolve_mcp_profile_path` keep writes under operator-chosen roots.
* **Private state.** Persistent browser profiles hold cookies and logged-in
  sessions. :func:`secure_directory` creates them ``0700`` so other local
  accounts cannot read them.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from webskrap.client import WebSkrapError

OUTPUT_DIR_ENV = "WEBSKRAP_OUTPUT_DIR"
DEFAULT_OUTPUT_DIRNAME = "webskrap-output"
MCP_PROFILE_DIR_ENV = "WEBSKRAP_MCP_PROFILE_DIR"
PRIVATE_DIR_MODE = 0o700


def output_root() -> Path:
    """Return the directory that model-supplied output paths are confined to.

    Defaults to ``./webskrap-output``; set ``WEBSKRAP_OUTPUT_DIR`` to move it.
    The directory is not created here — :func:`resolve_output_path` does that
    only once a destination has been accepted.
    """
    if override := os.environ.get(OUTPUT_DIR_ENV):
        return Path(override).expanduser()
    return Path.cwd() / DEFAULT_OUTPUT_DIRNAME


def mcp_profile_root() -> Path:
    """Return the root for model-supplied persistent browser profiles."""
    if override := os.environ.get(MCP_PROFILE_DIR_ENV):
        return Path(override).expanduser()
    return Path.home() / ".webskrap" / "profiles"


def resolve_mcp_profile_path(path: str | os.PathLike[str]) -> Path:
    """Resolve a model-supplied profile directory inside the MCP profile root.

    The Python API accepts unrestricted :class:`~pathlib.Path` values. This
    narrower helper exists for the MCP trust boundary, where page content can
    influence a model's tool arguments.

    Raises:
        WebSkrapError: If ``path`` is absolute, names the root itself, or
            resolves outside the configured profile root.
    """
    base = mcp_profile_root().expanduser()
    candidate = Path(path)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        msg = (
            f"profile path must be relative to {base}: '{candidate}' is absolute. "
            f"Pass a relative name, or set {MCP_PROFILE_DIR_ENV} to move the profile root."
        )
        raise WebSkrapError(msg)

    resolved_base = base.resolve()
    resolved = (resolved_base / candidate).resolve()
    if resolved == resolved_base or resolved_base not in resolved.parents:
        msg = (
            f"profile path must stay inside {base}: '{candidate}' escapes it. "
            f"Set {MCP_PROFILE_DIR_ENV} to store profiles elsewhere."
        )
        raise WebSkrapError(msg)

    try:
        secure_directory(resolved_base, tighten_existing=False)
        secure_directory(resolved)
    except OSError as exc:
        msg = f"could not create profile directory {resolved}: {exc}"
        raise WebSkrapError(msg) from exc
    return resolved


def resolve_output_path(
    path: str | os.PathLike[str] | None,
    *,
    root: Path | None = None,
    suffix: str = ".png",
) -> Path:
    """Resolve ``path`` to a writable destination inside ``root``.

    ``path`` is a *relative* destination: nested segments are allowed, and a
    generated name is used when it is None. Absolute paths and anything that
    resolves outside ``root`` (``..`` segments, symlinks pointing elsewhere)
    are rejected rather than normalized, so a caller that does not control the
    destination cannot be talked into writing anywhere else.

    Args:
        path: Relative destination under ``root``, or None for a generated name.
        root: Confinement root; defaults to :func:`output_root`.
        suffix: Extension used for generated names.

    Returns:
        The absolute destination. Its parent directory exists on return.

    Raises:
        WebSkrapError: If ``path`` is absolute, escapes ``root``, or names a
            directory that cannot be created.
    """
    base = (root or output_root()).expanduser()
    candidate = Path(path) if path is not None else Path(f"webskrap-{uuid4().hex}{suffix}")

    if candidate.is_absolute() or candidate.drive or candidate.root:
        msg = (
            f"output path must be relative to {base}: '{candidate}' is absolute. "
            f"Pass a relative name, or set {OUTPUT_DIR_ENV} to write elsewhere."
        )
        raise WebSkrapError(msg)
    if not candidate.name:
        msg = f"output path '{candidate}' does not name a file"
        raise WebSkrapError(msg)

    # Resolve both sides before comparing: '..' segments and symlinked
    # directories only show their real target after resolution.
    resolved_base = base.resolve()
    resolved = (resolved_base / candidate).resolve()
    if resolved_base not in resolved.parents:
        msg = (
            f"output path must stay inside {base}: '{candidate}' escapes it. "
            f"Set {OUTPUT_DIR_ENV} to write elsewhere."
        )
        raise WebSkrapError(msg)

    try:
        # Create the root owner-only when WebSkrap is the one creating it, so a
        # local attacker cannot plant symlinks inside the default output
        # directory. A root the user set up keeps the permissions they chose.
        secure_directory(resolved_base, tighten_existing=False)
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"could not create output directory {resolved.parent}: {exc}"
        raise WebSkrapError(msg) from exc
    return resolved


def secure_directory(path: Path, *, tighten_existing: bool = True) -> Path:
    """Create ``path`` (and parents) owner-only, optionally tightening it.

    ``mkdir(mode=...)`` is masked by the process umask and only applies to the
    final component, so the mode is re-applied explicitly. On non-POSIX
    platforms that step is skipped: Windows ignores these bits and inherits
    per-user ACLs from the profile directory instead.

    Args:
        path: Directory to create.
        tighten_existing: Also tighten a directory that already exists. Pass
            False for a directory the user chose and may share deliberately
            (a ``WEBSKRAP_BROWSER_DIR`` on a multi-user host); WebSkrap should
            not silently narrow permissions on a directory it did not create.
    """
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    if os.name == "posix" and (tighten_existing or not existed):
        _chmod_no_follow(path)
    return path


def _chmod_no_follow(path: Path) -> None:
    """Set ``path`` to ``0700`` without following a final symlink.

    ``Path.chmod`` resolves symlinks, so a symlink planted where a session
    directory belongs would hand the mode change to its target. Opening the
    directory with ``O_NOFOLLOW`` refuses that outright, and ``fchmod`` then
    acts on the directory actually opened.

    A directory the user deliberately symlinked elsewhere is therefore left
    alone rather than modified through the link, as is one owned by somebody
    else: neither is worth aborting a session over.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fchmod(fd, PRIVATE_DIR_MODE)
    finally:
        os.close(fd)
