"""The failure vocabulary every WebSkrap surface reports through.

A caller that cannot read prose -- an LLM agent driving the MCP tools, or a
script parsing ``--format json`` -- needs to know *which kind* of failure it
hit and what to do next, not just that something went wrong. Every failure is
therefore reduced to an :class:`ErrorCode`, and every code carries a fixed
recovery hint and a CLI exit status.

:class:`WebSkrapError` accepts an explicit code. Raise sites that predate this
module leave it unset, so :func:`classify` falls back to matching the message,
keeping one taxonomy for WebSkrap and Playwright failures alike.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """What kind of failure happened, and therefore what fixes it."""

    #: An argument was wrong: unknown action, bad literal, bad session name.
    USAGE = "usage"
    #: The named browser session has no running browser.
    NO_SESSION = "no_session"
    #: A session's state file exists but its browser cannot be reached.
    SESSION_UNREACHABLE = "session_unreachable"
    #: A snapshot ref no longer resolves; the DOM moved on.
    STALE_REF = "stale_ref"
    #: An action or navigation ran out of time.
    TIMEOUT = "timeout"
    #: The network refused, the host did not resolve, or the load failed.
    NAVIGATION = "navigation"
    #: No Chromium could be started.
    BROWSER_LAUNCH = "browser_launch"
    #: Chromium started but its OS sandbox could not.
    SANDBOX = "sandbox"
    #: A path was refused by the output or profile confinement rules.
    PATH_REJECTED = "path_rejected"
    #: Anything not yet classified.
    INTERNAL = "internal"


#: What a caller should do about each code. Kept short enough to sit in a tool
#: result without crowding out the payload it accompanies.
RECOVERY_HINTS: dict[ErrorCode, str] = {
    ErrorCode.USAGE: "Check the argument against the documented values and retry.",
    ErrorCode.NO_SESSION: (
        "Open the session first: browser_open (MCP) or `webskrap browser open` (CLI)."
    ),
    ErrorCode.SESSION_UNREACHABLE: (
        "The browser is gone but its state file remains. Close the session, then open it again."
    ),
    ErrorCode.STALE_REF: (
        "Refs describe one snapshot of the DOM. Take a fresh browser_snapshot and use its refs, "
        "or pass a Playwright selector instead."
    ),
    ErrorCode.TIMEOUT: (
        "Raise timeout_ms, wait for a weaker load state (domcontentloaded instead of "
        "networkidle), or wait for the element with browser_wait_for before acting on it."
    ),
    ErrorCode.NAVIGATION: "Check the URL, the scheme, and that the host is reachable.",
    ErrorCode.BROWSER_LAUNCH: (
        "Run `webskrap install`. On Linux ARM64 there is no Chrome build, so pass "
        "channel='chromium'."
    ),
    ErrorCode.SANDBOX: (
        "Chromium's OS sandbox could not start. Enable unprivileged user namespaces, or accept "
        "weaker renderer isolation with `webskrap browser open --no-sandbox` "
        "(WEBSKRAP_CHROMIUM_SANDBOX=0 for the MCP server)."
    ),
    ErrorCode.PATH_REJECTED: (
        "Paths are confined to a root. Pass a relative path, or move the root with "
        "WEBSKRAP_OUTPUT_DIR / WEBSKRAP_MCP_PROFILE_DIR."
    ),
    ErrorCode.INTERNAL: "Unexpected failure. Re-run with `webskrap doctor` to check the install.",
}

#: Process exit status per code, so a script can branch on the kind of failure
#: without parsing the message. 2 is left to the argument parser, which already
#: uses it for unknown options and malformed values.
EXIT_CODES: dict[ErrorCode, int] = {
    ErrorCode.INTERNAL: 1,
    ErrorCode.USAGE: 2,
    ErrorCode.TIMEOUT: 3,
    ErrorCode.NAVIGATION: 4,
    ErrorCode.BROWSER_LAUNCH: 5,
    ErrorCode.SANDBOX: 6,
    ErrorCode.NO_SESSION: 7,
    ErrorCode.SESSION_UNREACHABLE: 8,
    ErrorCode.STALE_REF: 9,
    ErrorCode.PATH_REJECTED: 10,
}

# Lowercased substrings that identify a failure whose raise site did not set a
# code. Order is significant: the sandbox hint is appended to a launch failure
# message, and a stale ref usually surfaces as a timeout on an `aria-ref=`
# locator, so the more specific marker has to be tested first.
_MESSAGE_CODES: tuple[tuple[str, ErrorCode], ...] = (
    ("sandbox could not start", ErrorCode.SANDBOX),
    ("no usable sandbox", ErrorCode.SANDBOX),
    ("setuid sandbox", ErrorCode.SANDBOX),
    ("--no-sandbox", ErrorCode.SANDBOX),
    ("aria-ref", ErrorCode.STALE_REF),
    ("is not open", ErrorCode.NO_SESSION),
    ("is not reachable", ErrorCode.SESSION_UNREACHABLE),
    ("invalid session name", ErrorCode.USAGE),
    ("unknown action", ErrorCode.USAGE),
    ("value argument", ErrorCode.USAGE),
    ("must be one of", ErrorCode.USAGE),
    ("must not be a symlink", ErrorCode.PATH_REJECTED),
    ("must be relative", ErrorCode.PATH_REJECTED),
    ("must stay inside", ErrorCode.PATH_REJECTED),
    ("does not name a file", ErrorCode.PATH_REJECTED),
    ("could not create", ErrorCode.PATH_REJECTED),
    ("executable doesn't exist", ErrorCode.BROWSER_LAUNCH),
    ("is not found at", ErrorCode.BROWSER_LAUNCH),
    ("playwright install", ErrorCode.BROWSER_LAUNCH),
    ("webskrap install", ErrorCode.BROWSER_LAUNCH),
    ("failed to launch", ErrorCode.BROWSER_LAUNCH),
    ("browsertype.launch", ErrorCode.BROWSER_LAUNCH),
    ("exited during startup", ErrorCode.BROWSER_LAUNCH),
    ("devtools port", ErrorCode.BROWSER_LAUNCH),
    ("chromium is not installed", ErrorCode.BROWSER_LAUNCH),
    ("net::err_", ErrorCode.NAVIGATION),
    ("err_name_not_resolved", ErrorCode.NAVIGATION),
    ("err_connection", ErrorCode.NAVIGATION),
    ("navigation failed", ErrorCode.NAVIGATION),
    ("timeout", ErrorCode.TIMEOUT),
    ("timed out", ErrorCode.TIMEOUT),
)


class WebSkrapError(RuntimeError):
    """Raised for every WebSkrap-level failure.

    Covers bad configuration, closed clients and sessions, missing browsers,
    and rejected paths. Playwright's own exceptions are left alone so callers
    can still catch a navigation timeout as a timeout; :func:`classify` gives
    both the same vocabulary.
    """

    def __init__(self, message: str, *, code: ErrorCode | None = None) -> None:
        """Record ``message``, optionally tagged with an explicit ``code``.

        Args:
            message: Human-readable, single-line description.
            code: Failure kind. Leave unset to have :func:`classify` derive one
                from the message.
        """
        super().__init__(message)
        self.code = code


def classify(exc: BaseException) -> ErrorCode:
    """Return the :class:`ErrorCode` that best describes ``exc``.

    An explicit code on a :class:`WebSkrapError` always wins. Otherwise the
    message is matched against :data:`_MESSAGE_CODES`, which also covers
    Playwright's own exceptions.
    """
    if isinstance(exc, WebSkrapError) and exc.code is not None:
        return exc.code
    message = str(exc).lower()
    for marker, code in _MESSAGE_CODES:
        if marker in message:
            return code
    return ErrorCode.INTERNAL


def first_line(exc: BaseException) -> str:
    """Return ``exc``'s first message line, or its type when it has none.

    Playwright errors carry a call log spanning dozens of lines. Only the first
    says what failed; the rest is noise in a tool result or a terminal.
    """
    lines = str(exc).strip().splitlines()
    return lines[0].strip() if lines and lines[0].strip() else type(exc).__name__


def error_payload(exc: BaseException) -> dict[str, str | bool]:
    """Return the machine-readable envelope for ``exc``.

    ``{"ok": False, "error": ..., "code": ..., "hint": ...}`` -- the same shape
    from the MCP tools and from every CLI command run with ``--format json``,
    so one parser handles both.
    """
    code = classify(exc)
    return {
        "ok": False,
        "error": first_line(exc),
        "code": str(code),
        "hint": RECOVERY_HINTS[code],
    }


def tool_message(exc: BaseException) -> str:
    """Return ``exc``'s message with its code and recovery hint folded in.

    MCP reports a failed tool call as text, so whatever the message does not
    say is not available to the caller at all. Attaching the code and the hint
    is what turns "Timeout 10000ms exceeded" into something a caller can act on
    without a second round trip.
    """
    code = classify(exc)
    return f"{first_line(exc)} [code: {code}] {RECOVERY_HINTS[code]}"


def exit_code(exc: BaseException) -> int:
    """Return the process exit status ``exc`` should terminate a CLI run with."""
    return EXIT_CODES[classify(exc)]
