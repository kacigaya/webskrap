"""Regex search over extracted page text, with a hard time limit.

Python's :mod:`re` backtracks, so a pattern such as ``(a+)+$`` can run for
minutes on a short line, and a running match cannot be interrupted. Patterns
come from callers (often a model reading hostile pages), so matching runs in a
child interpreter that is killed when :data:`PATTERN_TIMEOUT_S` runs out. The
pattern never runs in the page.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess  # nosec B404  # noqa: S404 - fixed argv, never a shell
import sys
from typing import Any

from webskrap.errors import ErrorCode, WebSkrapError

MAX_PATTERN_CHARS = 200
MAX_MATCHES = 50
MAX_CONTEXT_LINES = 3
#: Matched and context lines are clipped to this many characters.
MAX_LINE_CHARS = 300
PATTERN_TIMEOUT_S = 2.0

# Reads {"pattern", "lines"} on stdin; writes the indexes of matching lines.
_WORKER = """\
import json, re, sys
job = json.load(sys.stdin)
search = re.compile(job["pattern"]).search
json.dump([index for index, line in enumerate(job["lines"]) if search(line)], sys.stdout)
"""


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 1] + "…"


def validate_search(pattern: str, *, context: int, max_matches: int) -> None:
    """Reject an unusable pattern or out-of-range limits before any browser work.

    Raises:
        WebSkrapError: ``usage`` for a blank, overlong or invalid pattern, or
            a ``context`` / ``max_matches`` outside the documented caps.
    """
    if not pattern.strip():
        raise WebSkrapError("pattern cannot be blank", code=ErrorCode.USAGE)
    if len(pattern) > MAX_PATTERN_CHARS:
        msg = f"pattern is {len(pattern)} characters: keep it under {MAX_PATTERN_CHARS}"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    try:
        re.compile(pattern)
    except re.error as exc:
        raise WebSkrapError(f"invalid pattern: {exc}", code=ErrorCode.USAGE) from exc
    if not 0 <= context <= MAX_CONTEXT_LINES:
        msg = f"context must be between 0 and {MAX_CONTEXT_LINES}"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    if not 1 <= max_matches <= MAX_MATCHES:
        msg = f"max_matches must be between 1 and {MAX_MATCHES}"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)


async def _matching_lines(pattern: str, lines: list[str]) -> list[int]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        _WORKER,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    job = json.dumps({"pattern": pattern, "lines": lines}).encode()
    try:
        output, _ = await asyncio.wait_for(process.communicate(job), PATTERN_TIMEOUT_S)
    except TimeoutError:
        msg = (
            f"pattern took longer than {PATTERN_TIMEOUT_S:.0f}s; remove nested or "
            "overlapping quantifiers such as (a+)+"
        )
        raise WebSkrapError(msg, code=ErrorCode.USAGE) from None
    finally:
        # Also on cancellation: a backtracking match would otherwise keep running.
        if process.returncode is None:
            process.kill()
            await asyncio.shield(process.wait())
    if process.returncode != 0:
        raise WebSkrapError("pattern search failed", code=ErrorCode.INTERNAL)
    return [int(index) for index in json.loads(output)]


async def search_lines(
    text: str, pattern: str, *, context: int = 1, max_matches: int = 20
) -> dict[str, Any]:
    """Return the non-blank lines of ``text`` that ``pattern`` matches.

    Lines are stripped and blank ones dropped, so ``line`` numbers count
    non-blank lines from 1. Each match carries up to ``context`` lines
    before and after it. Call :func:`validate_search` first.

    Returns:
        ``{"matches": [{"line", "text", "before", "after"}], "matches_total",
        "matches_truncated"}``.

    Raises:
        WebSkrapError: ``usage`` when matching exceeds :data:`PATTERN_TIMEOUT_S`.
    """
    lines = [stripped for line in text.splitlines() if (stripped := line.strip())]
    hits = await _matching_lines(pattern, lines)
    matches = [
        {
            "line": index + 1,
            "text": _clip(lines[index]),
            "before": [_clip(line) for line in lines[max(0, index - context) : index]],
            "after": [_clip(line) for line in lines[index + 1 : index + 1 + context]],
        }
        for index in hits[:max_matches]
    ]
    return {
        "matches": matches,
        "matches_total": len(hits),
        "matches_truncated": len(hits) > max_matches,
    }
