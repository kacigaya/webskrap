from __future__ import annotations

import pytest

from webskrap.client import (
    WebSkrapError,
    WebSkrapSession,
    _bezier_path,
    _resource_route_handler,
)
from webskrap.models import ResourcePolicy, SessionConfig
from webskrap.profiles import get_profile


class _Request:
    def __init__(self, resource_type: str) -> None:
        self.resource_type = resource_type


class _Route:
    def __init__(self, resource_type: str) -> None:
        self.request = _Request(resource_type)
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _Mouse:
    def __init__(self) -> None:
        self.moves: list[tuple[float, float, int | None]] = []
        self.clicks: list[tuple[float, float, dict[str, object]]] = []

    async def move(self, x: float, y: float, *, steps: int | None = None) -> None:
        self.moves.append((x, y, steps))

    async def click(self, x: float, y: float, **options: object) -> None:
        self.clicks.append((x, y, options))


class _Keyboard:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def down(self, key: str) -> None:
        self.events.append(("down", key))

    async def up(self, key: str) -> None:
        self.events.append(("up", key))


class _Locator:
    def __init__(self, box: dict[str, float] | None = None, count: int = 1) -> None:
        self.box = box or {"x": 10, "y": 20, "width": 100, "height": 40}
        self.element_count = count
        self.waits: list[dict[str, object]] = []
        self.scrolled: list[dict[str, object]] = []

    async def wait_for(self, **options: object) -> None:
        self.waits.append(options)

    async def scroll_into_view_if_needed(self, **options: object) -> None:
        self.scrolled.append(options)

    async def bounding_box(self, **options: object) -> dict[str, float] | None:
        return self.box

    async def count(self) -> int:
        return self.element_count


class _Page:
    def __init__(self, locator: _Locator | None = None) -> None:
        self._locator = locator or _Locator()
        self.mouse = _Mouse()
        self.keyboard = _Keyboard()
        self.clicks: list[tuple[str, dict[str, object]]] = []
        self.evaluations: list[str] = []
        self.timeouts: list[float] = []
        self.locators: list[str] = []

    def locator(self, selector: str) -> _Locator:
        self.locators.append(selector)
        return self._locator

    async def click(self, selector: str, **options: object) -> None:
        self.clicks.append((selector, options))

    async def wait_for_timeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    async def evaluate(self, script: str) -> None:
        self.evaluations.append(script)


class _Response:
    status = 200
    headers = {"content-type": "text/html"}


class _BodyLocator:
    async def inner_text(self) -> str:
        return "Visible body"


class _FetchPage:
    url = "https://example.test/final"
    frames: list[object] = []

    def __init__(self) -> None:
        self.content_called = False
        self.closed = False
        self.locators: list[str] = []
        self.consent_waits: list[float] = []
        self.default_timeout: float | None = None
        self.default_navigation_timeout: float | None = None

    def set_default_timeout(self, timeout: float) -> None:
        self.default_timeout = timeout

    def set_default_navigation_timeout(self, timeout: float) -> None:
        self.default_navigation_timeout = timeout

    async def goto(self, *_args: object, **_kwargs: object) -> _Response:
        return _Response()

    async def wait_for_selector(self, _selector: str, *, timeout: float) -> object:
        self.consent_waits.append(timeout)
        raise TimeoutError("no cookie notice on this page")

    async def title(self) -> str:
        return "Example"

    async def content(self) -> str:
        self.content_called = True
        return "<html><body>Visible body</body></html>"

    def locator(self, selector: str) -> _BodyLocator:
        self.locators.append(selector)
        return _BodyLocator()

    async def close(self) -> None:
        self.closed = True


class _FetchContext:
    def __init__(self, page: _FetchPage) -> None:
        self.page = page

    async def new_page(self) -> _FetchPage:
        return self.page

    async def cookies(self) -> list[dict[str, object]]:
        return []


def _session() -> WebSkrapSession:
    return WebSkrapSession(
        name="test",
        context=None,  # type: ignore[arg-type]
        config=SessionConfig(),
        profile=get_profile(None),
    )


@pytest.mark.asyncio
async def test_lite_resource_policy_blocks_heavy_assets() -> None:
    handler = _resource_route_handler(ResourcePolicy.LITE)
    route = _Route("image")

    await handler(route)

    assert route.aborted is True
    assert route.continued is False


@pytest.mark.asyncio
async def test_lite_resource_policy_allows_documents() -> None:
    handler = _resource_route_handler(ResourcePolicy.LITE)
    route = _Route("document")

    await handler(route)

    assert route.aborted is False
    assert route.continued is True


@pytest.mark.asyncio
async def test_fetch_text_only_uses_body_inner_text() -> None:
    page = _FetchPage()
    session = WebSkrapSession(
        name="test",
        context=_FetchContext(page),  # type: ignore[arg-type]
        config=SessionConfig(),
        profile=get_profile(None),
    )

    result = await session.fetch("https://example.test", text_only=True)

    assert result.text == "Visible body"
    assert page.locators == ["body"]
    assert page.content_called is False
    assert page.closed is True
    assert result.cookie_notice_declined is None


@pytest.mark.asyncio
async def test_fetch_declines_cookie_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []

    async def fake_decline(_page: object, *, timeout_ms: float) -> str:
        timeouts.append(timeout_ms)
        return "cmp"

    monkeypatch.setattr("webskrap.client._decline_cookies", fake_decline)
    session = WebSkrapSession(
        name="test",
        context=_FetchContext(_FetchPage()),  # type: ignore[arg-type]
        config=SessionConfig(decline_cookies_timeout_ms=1234),
        profile=get_profile(None),
    )

    result = await session.fetch("https://example.test")

    assert result.cookie_notice_declined == "cmp"
    assert timeouts == [1234]


@pytest.mark.asyncio
async def test_fetch_skips_cookie_decline_when_disabled() -> None:
    page = _FetchPage()
    session = WebSkrapSession(
        name="test",
        context=_FetchContext(page),  # type: ignore[arg-type]
        config=SessionConfig(decline_cookies=False),
        profile=get_profile(None),
    )

    result = await session.fetch("https://example.test")

    assert result.cookie_notice_declined is None
    assert page.consent_waits == []


@pytest.mark.asyncio
async def test_human_click_false_delegates_to_page_click() -> None:
    page = _Page()

    await _session().human_click(
        page,  # type: ignore[arg-type]
        "label[for='radio1']",
        human=False,
        timeout=1000,
        strict=True,
    )

    assert page.clicks == [("label[for='radio1']", {"timeout": 1000, "strict": True})]
    assert page.mouse.clicks == []


@pytest.mark.asyncio
async def test_human_click_waits_moves_and_clicks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("webskrap.client.uniform", lambda _start, _end: 0)
    locator = _Locator()
    page = _Page(locator)

    await _session().human_click(
        page,  # type: ignore[arg-type]
        "label[for='radio1']",
        position={"x": 20, "y": 10},
        timeout=1500,
        button="left",
        click_count=1,
        delay=25,
        modifiers=["Shift"],
    )

    assert page.locators == ["label[for='radio1']"]
    assert locator.waits == [{"state": "visible", "timeout": 1500}]
    assert locator.scrolled == [{"timeout": 1500}]
    # uniform mocked to 0: start == end == (30, 30), so distance 0 -> 12 steps.
    assert page.mouse.moves[0] == (30, 30, 1)
    assert len(page.mouse.moves) == 1 + 12
    assert page.mouse.moves[-1] == (30, 30, 1)  # path lands exactly on target
    assert all(
        move[2] == 1 and abs(move[0] - 30) < 1e-6 and abs(move[1] - 30) < 1e-6
        for move in page.mouse.moves
    )
    # one settle wait + one wait per curve step + one final wait.
    assert page.timeouts == [0] * (1 + 12 + 1)
    assert page.mouse.clicks == [(30, 30, {"button": "left", "click_count": 1, "delay": 25})]
    assert page.keyboard.events == [("down", "Shift"), ("up", "Shift")]


def test_bezier_path_curves_and_lands_on_target() -> None:
    start, end = (0.0, 0.0), (200.0, 100.0)
    path = _bezier_path(start, end, 24)

    assert len(path) == 24
    assert path[-1] == end  # exact landing
    # at least one point bows off the straight start->end line (curved, not linear).
    dx, dy = end[0] - start[0], end[1] - start[1]

    def offline(p: tuple[float, float]) -> float:
        return abs(dx * (start[1] - p[1]) - (start[0] - p[0]) * dy)

    assert max(offline(p) for p in path) > 1.0


@pytest.mark.asyncio
async def test_human_click_trial_does_not_click() -> None:
    page = _Page()

    await _session().human_click(
        page,  # type: ignore[arg-type]
        "label[for='radio1']",
        trial=True,
    )

    assert page.mouse.moves == []
    assert page.mouse.clicks == []


@pytest.mark.asyncio
async def test_human_click_raises_for_missing_bounding_box() -> None:
    page = _Page(_Locator(box=None))
    page._locator.box = None

    with pytest.raises(WebSkrapError, match="visible bounding box"):
        await _session().human_click(page, "label[for='radio1']")  # type: ignore[arg-type]
