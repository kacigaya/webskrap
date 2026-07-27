from __future__ import annotations

import pytest

from webskrap.consent import (
    CMP_REJECT_CSS,
    REJECT_TEXT_PATTERN,
    decline_cookies,
)


class _Element:
    def __init__(self, *, visible: bool = True, clickable: bool = True) -> None:
        self.visible = visible
        self.clickable = clickable
        self.clicked = False

    async def is_visible(self) -> bool:
        return self.visible

    async def click(self, **_options: object) -> None:
        if not self.clickable:
            raise RuntimeError("element is covered")
        self.clicked = True


class _Locator:
    """Fake locator. ``elements`` is None to model a selector that never matches."""

    def __init__(self, elements: list[_Element] | None = None) -> None:
        self.elements = elements or []
        self.filtered_with: list[object] = []

    def locator(self, _selector: str) -> _Locator:
        return self

    def filter(self, *, has_text: object = None) -> _Locator:
        self.filtered_with.append(has_text)
        return self

    async def count(self) -> int:
        return len(self.elements)

    def nth(self, index: int) -> _Element:
        return self.elements[index]


class _Frame:
    def __init__(self, cmp: _Locator | None = None, text: _Locator | None = None) -> None:
        self.cmp = cmp or _Locator()
        self.text = text or _Locator()
        self.selectors: list[str] = []

    def locator(self, selector: str) -> _Locator:
        self.selectors.append(selector)
        return self.cmp if selector == CMP_REJECT_CSS else self.text


class _Page:
    def __init__(self, frames: list[_Frame], *, notice_found: bool = True) -> None:
        self.frames = frames
        self.notice_found = notice_found
        self.waited_selector: str | None = None
        self.waited_timeout: float | None = None
        self.settled = False

    async def wait_for_selector(self, selector: str, *, timeout: float) -> object:
        self.waited_selector = selector
        self.waited_timeout = timeout
        if not self.notice_found:
            raise TimeoutError("no consent notice")
        return object()

    async def wait_for_timeout(self, _timeout: float) -> None:
        self.settled = True


@pytest.mark.asyncio
async def test_clicks_known_cmp_reject_button() -> None:
    button = _Element()
    page = _Page([_Frame(cmp=_Locator([button]))])

    assert await decline_cookies(page) == "cmp"
    assert button.clicked is True
    assert page.settled is True


@pytest.mark.asyncio
async def test_falls_back_to_reject_text_inside_consent_container() -> None:
    button = _Element()
    frame = _Frame(text=_Locator([button]))
    page = _Page([frame])

    assert await decline_cookies(page) == "text"
    assert button.clicked is True
    assert frame.text.filtered_with == [REJECT_TEXT_PATTERN]


@pytest.mark.asyncio
async def test_skips_hidden_and_unclickable_candidates() -> None:
    hidden = _Element(visible=False)
    covered = _Element(clickable=False)
    real = _Element()
    page = _Page([_Frame(cmp=_Locator([hidden, covered, real]))])

    assert await decline_cookies(page) == "cmp"
    assert (hidden.clicked, covered.clicked, real.clicked) == (False, False, True)


@pytest.mark.asyncio
async def test_searches_every_frame() -> None:
    button = _Element()
    page = _Page([_Frame(), _Frame(cmp=_Locator([button]))])

    assert await decline_cookies(page) == "cmp"
    assert button.clicked is True


@pytest.mark.asyncio
async def test_returns_none_when_no_notice_appears() -> None:
    button = _Element()
    page = _Page([_Frame(cmp=_Locator([button]))], notice_found=False)

    assert await decline_cookies(page, timeout_ms=500) is None
    assert page.waited_timeout == 500
    assert button.clicked is False


@pytest.mark.asyncio
async def test_zero_timeout_skips_the_wait() -> None:
    button = _Element()
    page = _Page([_Frame(cmp=_Locator([button]))], notice_found=False)

    assert await decline_cookies(page, timeout_ms=0) == "cmp"
    assert page.waited_selector is None


@pytest.mark.asyncio
async def test_returns_none_when_nothing_matches() -> None:
    page = _Page([_Frame()])

    assert await decline_cookies(page) is None
    assert page.settled is False


@pytest.mark.parametrize(
    "label",
    [
        "Reject all",
        "Decline",
        "Refuse all cookies",
        "Deny",
        "Continue without accepting",
        "Only essential cookies",
        "Necessary cookies only",
        "Tout refuser",
        "Alle ablehnen",
        "Rechazar todo",
        "Rejeitar tudo",
        "Rifiuta tutto",
        "Alles weigeren",
        "Odrzuć wszystko",
        "拒否する",
        "거부",
    ],
)
def test_reject_text_pattern_matches_reject_labels(label: str) -> None:
    assert REJECT_TEXT_PATTERN.search(label) is not None


@pytest.mark.parametrize(
    "label",
    ["Accept all", "Allow cookies", "Manage preferences", "Learn more", "Sign in", "OK"],
)
def test_reject_text_pattern_ignores_other_labels(label: str) -> None:
    assert REJECT_TEXT_PATTERN.search(label) is None
