from __future__ import annotations

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from webskrap import browser_session
from webskrap.errors import (
    EXIT_CODES,
    RECOVERY_HINTS,
    ErrorCode,
    WebSkrapError,
    classify,
    error_payload,
    exit_code,
    first_line,
)


def test_every_code_has_a_hint_and_an_exit_status() -> None:
    assert set(RECOVERY_HINTS) == set(ErrorCode)
    assert set(EXIT_CODES) == set(ErrorCode)
    # Exit statuses must be distinguishable, and must leave 0 to success.
    assert len(set(EXIT_CODES.values())) == len(EXIT_CODES)
    assert 0 not in EXIT_CODES.values()


def test_explicit_code_wins_over_the_message() -> None:
    error = WebSkrapError("timeout while doing something", code=ErrorCode.USAGE)

    assert classify(error) is ErrorCode.USAGE


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param("session 'default' is not open", ErrorCode.NO_SESSION, id="no-session"),
        pytest.param(
            "session 'default' is not reachable", ErrorCode.SESSION_UNREACHABLE, id="unreachable"
        ),
        pytest.param(
            'Timeout 10000ms exceeded waiting for locator("aria-ref=e15")',
            ErrorCode.STALE_REF,
            id="stale-ref-beats-timeout",
        ),
        pytest.param("Timeout 30000ms exceeded", ErrorCode.TIMEOUT, id="timeout"),
        pytest.param("net::ERR_NAME_NOT_RESOLVED", ErrorCode.NAVIGATION, id="navigation"),
        pytest.param(
            "Executable doesn't exist at /root/.cache", ErrorCode.BROWSER_LAUNCH, id="launch"
        ),
        pytest.param("invalid session name '.'", ErrorCode.USAGE, id="usage"),
        pytest.param("output path 'x' must stay inside /out", ErrorCode.PATH_REJECTED, id="path"),
        pytest.param("something nobody predicted", ErrorCode.INTERNAL, id="fallback"),
    ],
)
def test_untagged_messages_are_classified(message: str, expected: ErrorCode) -> None:
    assert classify(WebSkrapError(message)) is expected


def test_a_sandbox_failure_outranks_the_launch_failure_wrapping_it() -> None:
    # `_wait_for_devtools_port` appends the sandbox hint to a launch message;
    # the sandbox is the actionable half, so it must win.
    message = (
        "browser exited during startup; see /tmp/browser.log. Chromium's sandbox could not start. "
        "Enable unprivileged user namespaces"
    )

    assert classify(WebSkrapError(message)) is ErrorCode.SANDBOX


def test_playwright_errors_are_classified_too() -> None:
    assert classify(PlaywrightTimeoutError("Timeout 5000ms exceeded")) is ErrorCode.TIMEOUT


def test_first_line_drops_the_playwright_call_log() -> None:
    error = PlaywrightTimeoutError("Timeout 5000ms exceeded\nCall log:\n  - waiting for locator")

    assert first_line(error) == "Timeout 5000ms exceeded"


def test_first_line_falls_back_to_the_exception_type() -> None:
    assert first_line(RuntimeError("")) == "RuntimeError"


def test_error_payload_is_the_shared_envelope() -> None:
    payload = error_payload(WebSkrapError("session 'a' is not open"))

    assert payload == {
        "ok": False,
        "error": "session 'a' is not open",
        "code": "no_session",
        "hint": RECOVERY_HINTS[ErrorCode.NO_SESSION],
    }


def test_exit_code_maps_the_classification() -> None:
    assert exit_code(WebSkrapError("session 'a' is not open")) == EXIT_CODES[ErrorCode.NO_SESSION]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param(".", ErrorCode.USAGE, id="dot-name"),
        pytest.param("has/slash", ErrorCode.USAGE, id="separator"),
    ],
)
def test_session_name_rejection_is_tagged_at_the_raise_site(name: str, expected: ErrorCode) -> None:
    with pytest.raises(WebSkrapError) as caught:
        browser_session.session_dir(name)

    assert caught.value.code is expected


def test_arity_rejection_is_tagged_at_the_raise_site() -> None:
    with pytest.raises(WebSkrapError) as caught:
        browser_session.element_arguments("fill", [])

    assert caught.value.code is ErrorCode.USAGE
