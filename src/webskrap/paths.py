"""Filesystem policy for paths WebSkrap does not fully control.

Two concerns live here:

* **Output confinement.** The MCP server takes file destinations from a model,
  which in turn reads untrusted pages, so a screenshot destination is not a
  trusted input. :func:`resolve_output_path` keeps those writes under one root.
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
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"could not create output directory {resolved.parent}: {exc}"
        raise WebSkrapError(msg) from exc
    return resolved


def secure_directory(path: Path, *, tighten_existing: bool = True) -> Path:
    """Create ``path`` (and parents) owner-only, optionally tightening it.

    ``mkdir(mode=...)`` is masked by the process umask and only applies to the
    final component, so the mode is re-applied explicitly. On non-POSIX
    platforms the chmod is skipped: Windows ignores these bits and inherits
    per-user ACLs from the profile directory instead.

    Args:
        path: Directory to create.
        tighten_existing: Also chmod a directory that already exists. Pass
            False for a directory the user chose and may share deliberately
            (a ``WEBSKRAP_BROWSER_DIR`` on a multi-user host); WebSkrap should
            not silently narrow permissions on a directory it did not create.
    """
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    if os.name == "posix" and (tighten_existing or not existed):
        # A directory created before this policy (or under a loose umask) still
        # needs tightening; failing to chmod someone else's directory is not
        # worth aborting the session for.
        with suppress(OSError):
            path.chmod(PRIVATE_DIR_MODE)
    return path
