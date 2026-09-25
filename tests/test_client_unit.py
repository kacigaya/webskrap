from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from webskrap.client import (
    WebSkrapClient,
    WebSkrapError,
    WebSkrapSession,
    _resource_route_handler,
    browser_doctor,
    lavapipe_available,
)
from webskrap.consent import SETTLED_PAGE_TIMEOUT_MS
from webskrap.errors import ErrorCode
from webskrap.human import bezier_path, scroll_into_view
from webskrap.models import ResourcePolicy, SearchEngine, SessionConfig
from webskrap.profiles import get_profile

SEARCH_FIXTURES = Path(__file__).parent / "fixtures" / "search"


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
        self.wheels: list[tuple[float, float]] = []

    async def move(self, x: float, y: float, *, steps: int | None = None) -> None:
        self.moves.append((x, y, steps))

    async def click(self, x: float, y: float, **options: object) -> None:
        self.clicks.append((x, y, options))

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        self.wheels.append((delta_x, delta_y))


class _Keyboard:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def down(self, key: str) -> None:
        self.events.append(("down", key))

    async def up(self, key: str) -> None:
        self.events.append(("up", key))


class _Locator:
    def __init__(
        self, box: dict[str, float] | None = None, count: int = 1, *, receives_click: bool = True
    ) -> None:
        self.box = box or {"x": 10, "y": 20, "width": 100, "height": 40}
        self.element_count = count
        self.receives_click = receives_click
        self.hit_tests: list[object] = []
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

    async def evaluate(self, _script: str, arg: object) -> bool:
        self.hit_tests.append(arg)
        return self.receives_click


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

    async def evaluate(self, script: str) -> object:
        self.evaluations.append(script)
        if "innerHeight" in script:
            return [1280, 720]
        return None


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
        config=SessionConfig(decline_cookies=True, decline_cookies_timeout_ms=1234),
        profile=get_profile(None),
    )

    result = await session.fetch("https://example.test")

    assert result.cookie_notice_declined == "cmp"
    assert timeouts == [1234]


@pytest.mark.asyncio
async def test_networkidle_shrinks_the_decline_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []

    async def fake_decline(_page: object, *, timeout_ms: float) -> None:
        timeouts.append(timeout_ms)
        return None

    monkeypatch.setattr("webskrap.client._decline_cookies", fake_decline)
    session = WebSkrapSession(
        name="test",
        context=_FetchContext(_FetchPage()),  # type: ignore[arg-type]
        config=SessionConfig(decline_cookies=True, decline_cookies_timeout_ms=5_000),
        profile=get_profile(None),
    )

    await session.fetch("https://example.test", wait_until="networkidle")
    await session.fetch("https://example.test", wait_until="domcontentloaded")

    # networkidle already waited out the CMP script; do not wait for it twice.
    assert timeouts == [SETTLED_PAGE_TIMEOUT_MS, 5_000]


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


class _SearchPage(_FetchPage):
    """A fetch page that serves a saved results page and records the URL."""

    def __init__(self, fixture: str) -> None:
        super().__init__()
        self.html = (SEARCH_FIXTURES / fixture).read_text(encoding="utf-8")
        self.requested: list[str] = []
        self.ready_waits: list[float] = []

    async def goto(self, url: str, **_kwargs: object) -> _Response:
        self.requested.append(url)
        return _Response()

    async def wait_for_selector(self, selector: str, *, timeout: float, **_state: object) -> object:
        if selector == "body > *":
            self.ready_waits.append(timeout)
            return object()
        return await super().wait_for_selector(selector, timeout=timeout)

    async def content(self) -> str:
        return self.html


def _search_session(page: _SearchPage, **config: object) -> WebSkrapSession:
    return WebSkrapSession(
        name="test",
        context=_FetchContext(page),  # type: ignore[arg-type]
        config=SessionConfig(**config),  # type: ignore[arg-type]
        profile=get_profile(None),
    )


@pytest.mark.asyncio
async def test_search_loads_the_results_page_and_caps_the_hits() -> None:
    page = _SearchPage("bing.html")

    result = await _search_session(page).search("example domain", max_results=2)

    assert page.requested == ["https://www.bing.com/search?q=example+domain"]
    # The body wait covers Bing's head-only redirect interstitial; it is on
    # the navigation budget, not the consent one.
    assert page.ready_waits == [SessionConfig().navigation_timeout_ms]
    assert page.closed is True
    assert result.query == "example domain"
    assert "elapsed_ms" in result.timings
    assert result.engine is SearchEngine.BING
    assert result.url == page.requested[0]
    assert result.status == 200
    assert result.ok is True
    assert [hit.url for hit in result.hits] == [
        "https://example.com/",
        "https://www.iana.org/help/example-domains",
    ]
    assert result.hits_total == 3


@pytest.mark.asyncio
async def test_search_selects_the_engine() -> None:
    page = _SearchPage("ddg.html")

    result = await _search_session(page).search("example domain", engine=SearchEngine.DDG)

    assert page.requested == ["https://html.duckduckgo.com/html/?q=example+domain"]
    assert result.engine is SearchEngine.DDG
    assert result.hits_total == 4


@pytest.mark.asyncio
async def test_search_goes_through_the_consent_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_decline(_page: object, *, timeout_ms: float) -> str:
        return "cmp"

    monkeypatch.setattr("webskrap.client._decline_cookies", fake_decline)
    session = _search_session(_SearchPage("bing.html"), decline_cookies=True)

    result = await session.search("example domain", engine=SearchEngine.BING)

    assert result.cookie_notice_declined == "cmp"


@pytest.mark.asyncio
async def test_search_reports_a_challenge_page_as_blocked() -> None:
    with pytest.raises(WebSkrapError) as excinfo:
        await _search_session(_SearchPage("bing_challenge.html")).search("example domain")

    assert excinfo.value.code is ErrorCode.BLOCKED


@pytest.mark.asyncio
async def test_search_rejects_a_blank_query_before_opening_a_page() -> None:
    page = _SearchPage("bing.html")

    with pytest.raises(WebSkrapError) as excinfo:
        await _search_session(page).search("   ")

    assert excinfo.value.code is ErrorCode.USAGE
    assert page.requested == []


@pytest.mark.asyncio
async def test_search_on_a_closed_session_fails() -> None:
    session = _search_session(_SearchPage("bing.html"))
    session._closed = True

    with pytest.raises(WebSkrapError, match="is closed"):
        await session.search("example domain")


@pytest.mark.asyncio
async def test_client_search_uses_a_throwaway_session(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _SearchPage("bing.html")
    session = _search_session(page)
    closed: list[str] = []

    async def fake_close() -> None:
        closed.append(session.name)
        session._closed = True

    monkeypatch.setattr(session, "close", fake_close)
    monkeypatch.setattr("webskrap.client._async_playwright", lambda _driver: _Manager())
    client = WebSkrapClient()
    monkeypatch.setattr(client, "_create_session", lambda *_args: _resolved(session))

    result = await client.search("example domain", max_results=1)
    await client.close()

    assert [hit.url for hit in result.hits] == ["https://example.com/"]
    assert result.hits_total == 3
    assert result.engine is SearchEngine.BING
    assert page.requested == ["https://www.bing.com/search?q=example+domain"]
    assert closed == [session.name]
    assert client._sessions == {}


async def _resolved(session: WebSkrapSession) -> WebSkrapSession:
    return session


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
    monkeypatch.setattr("webskrap.human.uniform", lambda _start, _end: 0)
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
    # The box starts 20px from the top, inside the view margin, but aiming
    # it lower needs less than one wheel notch, so the wheel stays still and
    # Playwright's scroll finishes (one pause); then one settle wait, one
    # wait per curve step and one final wait.
    assert page.mouse.wheels == []
    assert page.timeouts == [0] * (1 + 1 + 12 + 1)
    assert page.mouse.clicks == [(30, 30, {"button": "left", "click_count": 1, "delay": 25})]
    assert page.keyboard.events == [("down", "Shift"), ("up", "Shift")]


def test_bezier_path_curves_and_lands_on_target() -> None:
    start, end = (0.0, 0.0), (200.0, 100.0)
    path = bezier_path(start, end, 24)

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


class _ManagedSession:
    def __init__(self) -> None:
        self._closed = False

    async def close(self) -> None:
        self._closed = True


class _Playwright:
    async def stop(self) -> None:
        return None


class _Manager:
    async def start(self) -> _Playwright:
        return _Playwright()


@pytest.mark.asyncio
async def test_session_config_selects_the_started_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    drivers: list[str] = []

    def fake_factory(driver: str) -> _Manager:
        drivers.append(driver)
        return _Manager()

    async def fake_create(*_args: object) -> _ManagedSession:
        return _ManagedSession()

    monkeypatch.setattr("webskrap.client._async_playwright", fake_factory)
    client = WebSkrapClient()
    monkeypatch.setattr(client, "_create_session", fake_create)

    await client.session("stealth", config=SessionConfig(driver="patchright"))
    await client.close()

    assert drivers == ["patchright"]


@pytest.mark.asyncio
async def test_closed_named_session_is_recreated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("webskrap.client._async_playwright", lambda _driver: _Manager())
    client = WebSkrapClient()
    monkeypatch.setattr(client, "_create_session", lambda *_args: _new_managed_session())

    first = await client.session("reopen")
    await first.close()
    second = await client.session("reopen")
    await client.close()

    assert second is not first


@pytest.mark.asyncio
async def test_concurrent_named_session_is_created_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("webskrap.client._async_playwright", lambda _driver: _Manager())
    client = WebSkrapClient()
    started = asyncio.Event()
    release = asyncio.Event()
    created = 0

    async def create(*_args: object) -> _ManagedSession:
        nonlocal created
        created += 1
        started.set()
        await release.wait()
        return _ManagedSession()

    monkeypatch.setattr(client, "_create_session", create)
    first_task = asyncio.create_task(client.session("shared"))
    await started.wait()
    second_task = asyncio.create_task(client.session("shared"))
    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(first_task, second_task)
    await client.close()

    assert first is second
    assert created == 1


@pytest.mark.asyncio
async def test_close_cleans_session_that_is_still_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("webskrap.client._async_playwright", lambda _driver: _Manager())
    client = WebSkrapClient()
    started = asyncio.Event()
    release = asyncio.Event()
    created = _ManagedSession()

    async def create(*_args: object) -> _ManagedSession:
        started.set()
        await release.wait()
        return created

    monkeypatch.setattr(client, "_create_session", create)
    session_task = asyncio.create_task(client.session("closing"))
    await started.wait()
    close_task = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(WebSkrapError, match="closed while the session was starting"):
        await session_task
    await close_task

    assert created._closed is True


@pytest.mark.asyncio
async def test_close_stops_driver_that_is_still_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowPlaywright(_Playwright):
        stopped = False

        async def stop(self) -> None:
            self.stopped = True

    class SlowManager:
        async def start(self) -> SlowPlaywright:
            started.set()
            await release.wait()
            return playwright

    playwright = SlowPlaywright()
    monkeypatch.setattr("webskrap.client._async_playwright", lambda _driver: SlowManager())
    client = WebSkrapClient()
    session_task = asyncio.create_task(client.session("starting"))
    await started.wait()
    close_task = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(WebSkrapError, match="driver was starting"):
        await session_task
    await close_task

    assert playwright.stopped is True
    assert client._playwright is None


async def _new_managed_session() -> _ManagedSession:
    return _ManagedSession()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        pytest.param("file:///etc/passwd", id="file"),
        pytest.param("ftp://example.test/x", id="ftp"),
        pytest.param("https://user:pass@example.test/", id="userinfo"),
    ],
)
async def test_fetch_rejects_unfetchable_targets_without_a_page(url: str) -> None:
    # Validation runs before new_page, so a session without a context proves
    # no browser work starts for a rejected URL.
    session = _session()

    with pytest.raises(WebSkrapError) as caught:
        await session.fetch(url)

    assert caught.value.code is ErrorCode.USAGE


class _LaunchContext:
    def __init__(self) -> None:
        self.closed = False

    def set_default_timeout(self, _timeout: float) -> None:
        pass

    def set_default_navigation_timeout(self, _timeout: float) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class _LaunchChromium:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.options: dict[str, object] = {}

    async def launch_persistent_context(self, _dir: str, **options: object) -> _LaunchContext:
        self.options = options
        if self.fail:
            msg = "browserType.launchPersistentContext: failed to launch"
            raise RuntimeError(msg)
        return _LaunchContext()


class _FakeDisplay:
    def __init__(self) -> None:
        self.stopped = 0
        self.env = {"DISPLAY": ":99", "XAUTHORITY": "/tmp/webskrap-display-x/Xauthority"}

    async def stop(self) -> None:
        self.stopped += 1


def _virtual_display_client(
    monkeypatch: pytest.MonkeyPatch, chromium: _LaunchChromium
) -> tuple[WebSkrapClient, list[_FakeDisplay], list[tuple[int, int]]]:
    displays: list[_FakeDisplay] = []
    sizes: list[tuple[int, int]] = []

    async def fake_start(width: int, height: int) -> _FakeDisplay:
        sizes.append((width, height))
        displays.append(_FakeDisplay())
        return displays[-1]

    async def no_probe(*_args: object) -> str:
        raise AssertionError("the UA probe must not run under a virtual display")

    monkeypatch.setattr("webskrap.client.VirtualDisplay.start", fake_start)
    client = WebSkrapClient()
    monkeypatch.setattr(client, "_headless_clean_user_agent", no_probe)
    client._playwright = type("_PW", (), {"chromium": chromium})()
    return client, displays, sizes


@pytest.mark.asyncio
async def test_virtual_display_session_runs_headed_on_the_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chromium = _LaunchChromium()
    client, displays, sizes = _virtual_display_client(monkeypatch, chromium)
    config = SessionConfig(driver="patchright", virtual_display=True, mask_headless_user_agent=True)

    session = await client._create_session("vd", config, get_profile("desktop-chrome"))

    env = chromium.options["env"]
    assert isinstance(env, dict)
    assert env["DISPLAY"] == ":99"
    assert env["XAUTHORITY"].endswith("Xauthority")
    assert "PATH" in env  # the rest of the environment survives
    assert chromium.options["headless"] is False
    assert not any(str(a).startswith("--user-agent") for a in chromium.options["args"])
    assert sizes == [(1920, 1080)]

    await session.close()
    assert displays[0].stopped == 1


@pytest.mark.asyncio
async def test_virtual_display_is_stopped_when_launch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, displays, _sizes = _virtual_display_client(monkeypatch, _LaunchChromium(fail=True))
    config = SessionConfig(driver="patchright", virtual_display=True)

    with pytest.raises(RuntimeError, match="failed to launch"):
        await client._create_session("vd", config, get_profile("desktop-chrome"))

    assert displays[0].stopped == 1


@pytest.mark.asyncio
async def test_virtual_display_keeps_the_native_profile_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    chromium = _LaunchChromium()
    client, _displays, _sizes = _virtual_display_client(monkeypatch, chromium)
    config = SessionConfig(
        driver="patchright", virtual_display=True, patchright_context_profile=True
    )

    session = await client._create_session("vd", config, get_profile("desktop-chrome"))
    await session.close()

    env = chromium.options["env"]
    assert isinstance(env, dict)
    assert env["DISPLAY"] == ":99"
    assert env["TZ"] == "Europe/Paris"
    assert env["LANG"] == "en_US.UTF-8"


@pytest.mark.asyncio
async def test_unknown_profile_timezone_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    client = WebSkrapClient()
    client._playwright = type("_PW", (), {"chromium": _LaunchChromium()})()
    config = SessionConfig(driver="patchright", patchright_context_profile=True)
    profile = get_profile("desktop-chrome").model_copy(update={"timezone_id": "Mars/Olympus"})

    with pytest.raises(WebSkrapError, match="unknown timezone") as excinfo:
        await client._create_session("tz", config, profile)

    assert excinfo.value.code is ErrorCode.USAGE


@pytest.mark.asyncio
async def test_doctor_hints_at_the_sandbox_when_that_is_what_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launches: list[dict[str, object]] = []

    class _Chromium:
        executable_path = "/browsers/chromium"

        async def launch(self, **options: object) -> None:
            launches.append(options)
            raise RuntimeError(
                "BrowserType.launch: closed\nBrowser logs:\nChromium sandboxing failed!"
            )

    class _PW:
        chromium = _Chromium()

        async def stop(self) -> None:
            pass

    class _Starter:
        async def start(self) -> _PW:
            return _PW()

    monkeypatch.setattr("webskrap.client._async_playwright", lambda _driver: _Starter())

    report = await browser_doctor(chromium_sandbox=True)

    assert report["ok"] is False
    assert all(options["chromium_sandbox"] is True for options in launches)
    assert [options["channel"] for options in launches] == ["chrome", "msedge", "chromium"]
    assert "--no-sandbox" in str(report["hint"])


@pytest.mark.asyncio
async def test_doctor_tries_edge_before_bundled_chromium(monkeypatch: pytest.MonkeyPatch) -> None:
    channels: list[str | None] = []

    class _Browser:
        async def close(self) -> None:
            pass

    class _Chromium:
        executable_path = "/browsers/chromium"

        async def launch(self, **options: object) -> _Browser:
            channel = options["channel"]
            channels.append(channel if isinstance(channel, str) else None)
            if channel == "chrome":
                raise RuntimeError("Chrome not installed")
            return _Browser()

    class _PW:
        chromium = _Chromium()

        async def stop(self) -> None:
            pass

    class _Starter:
        async def start(self) -> _PW:
            return _PW()

    monkeypatch.setattr("webskrap.client._async_playwright", lambda _driver: _Starter())

    report = await browser_doctor()

    assert report["ok"] is True
    assert report["channel"] == "msedge"
    assert channels == ["chrome", "msedge"]


def test_lavapipe_is_found_by_its_vulkan_icd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("VK_DRIVER_FILES", raising=False)
    monkeypatch.delenv("VK_ICD_FILENAMES", raising=False)
    icd_dir = tmp_path / "icd.d"
    icd_dir.mkdir()
    monkeypatch.setattr("webskrap.client.VULKAN_ICD_DIRS", (tmp_path / "missing", icd_dir))

    assert not lavapipe_available()
    (icd_dir / "lvp_icd.aarch64.json").write_text("{}")
    assert lavapipe_available()


def test_lavapipe_is_never_available_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")

    assert not lavapipe_available()


@pytest.mark.asyncio
async def test_mesa_gpu_without_lavapipe_fails_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chromium = _LaunchChromium()
    client, displays, _sizes = _virtual_display_client(monkeypatch, chromium)
    monkeypatch.setattr("webskrap.client.lavapipe_available", lambda: False)
    config = SessionConfig(driver="patchright", gpu="mesa", virtual_display=True)

    with pytest.raises(WebSkrapError, match="mesa-vulkan-drivers") as excinfo:
        await client._create_session("gpu", config, get_profile("desktop-chrome"))

    assert excinfo.value.code is ErrorCode.BROWSER_LAUNCH
    assert chromium.options == {}  # the browser was never launched
    assert displays == []  # nor the display


@pytest.mark.asyncio
async def test_human_click_holds_the_button_for_a_human_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Playwright's own click releases after ~2 ms; unset, the hold is drawn
    # from 60-140 ms. uniform() returning its lower bound pins it to 60.
    monkeypatch.setattr("webskrap.human.uniform", lambda start, _end: start)
    page = _Page()

    await _session().human_click(page, "button")  # type: ignore[arg-type]

    assert page.mouse.clicks[0][2] == {"delay": 60}


class _ScrollingPage(_Page):
    """A page whose wheel moves the locator's box, like a scrolling document."""

    def __init__(self, box_y: float, scrolls: bool = True) -> None:
        super().__init__(_Locator(box={"x": 100, "y": box_y, "width": 80, "height": 30}))
        self._scrolls = scrolls
        page = self

        async def wheel(delta_x: float, delta_y: float) -> None:
            page.mouse.wheels.append((delta_x, delta_y))
            if page._scrolls:
                page._locator.box["y"] -= delta_y

        self.mouse.wheel = wheel  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_scroll_wheels_a_far_element_into_view() -> None:
    page = _ScrollingPage(box_y=2400)

    await scroll_into_view(page, page._locator)  # type: ignore[arg-type]

    deltas = [dy for _dx, dy in page.mouse.wheels]
    assert deltas and all(0 < dy <= 120 for dy in deltas)  # notch-sized, downward
    assert 40 <= page._locator.box["y"] <= 720 - 40 - 30
    assert page._locator.scrolled == []  # no programmatic jump needed


@pytest.mark.asyncio
async def test_scroll_wheels_up_to_an_element_above() -> None:
    page = _ScrollingPage(box_y=-900)

    await scroll_into_view(page, page._locator)  # type: ignore[arg-type]

    assert all(dy < 0 for _dx, dy in page.mouse.wheels)
    assert page._locator.scrolled == []


@pytest.mark.asyncio
async def test_scroll_leaves_a_visible_element_alone() -> None:
    page = _ScrollingPage(box_y=300)

    await scroll_into_view(page, page._locator)  # type: ignore[arg-type]

    assert page.mouse.wheels == []
    assert page._locator.scrolled == []


@pytest.mark.asyncio
async def test_scroll_falls_back_when_the_wheel_moves_nothing() -> None:
    # e.g. the element sits in an inner scroll container the cursor is not over.
    page = _ScrollingPage(box_y=2400, scrolls=False)

    await scroll_into_view(page, page._locator, timeout=500)  # type: ignore[arg-type]

    assert page.mouse.wheels  # it tried the wheel first
    assert page._locator.scrolled == [{"timeout": 500}]


class _TypingKeyboard(_Keyboard):
    def __init__(self) -> None:
        super().__init__()
        self.typed: list[tuple[str, float]] = []

    async def type(self, text: str, *, delay: float) -> None:
        self.typed.append((text, delay))


@pytest.mark.asyncio
async def test_human_type_clicks_then_types_each_key_with_human_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # uniform() returning its upper bound: hold 90 ms, and the "long pause"
    # draw (1.0) never beats 1/12, so every gap is the 160 ms upper bound.
    monkeypatch.setattr("webskrap.human.uniform", lambda _start, end: end)
    page = _Page(_Locator(box={"x": 10, "y": 200, "width": 100, "height": 30}))
    page.keyboard = _TypingKeyboard()

    await _session().human_type(page, "input[name=q]", "hé!")  # type: ignore[arg-type]

    assert len(page.mouse.clicks) == 1  # focused with a humanized click
    assert page.keyboard.typed == [("h", 90), ("é", 90), ("!", 90)]
    assert page.timeouts[-3:] == [160, 160, 160]


@pytest.mark.asyncio
async def test_human_type_pauses_now_and_then(monkeypatch: pytest.MonkeyPatch) -> None:
    # A draw of 0 lands under 1/12, so each gap is a long 250-600 ms pause.
    monkeypatch.setattr("webskrap.human.uniform", lambda start, _end: start)
    page = _Page(_Locator(box={"x": 10, "y": 200, "width": 100, "height": 30}))
    page.keyboard = _TypingKeyboard()

    await _session().human_type(page, "input", "ab")  # type: ignore[arg-type]

    assert page.timeouts[-2:] == [250, 250]


@pytest.mark.asyncio
async def test_human_type_on_a_closed_session_fails() -> None:
    session = _session()
    session._closed = True

    with pytest.raises(WebSkrapError, match="is closed"):
        await session.human_type(_Page(), "input", "x")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_human_click_refuses_a_covered_element() -> None:
    locator = _Locator(box={"x": 10, "y": 200, "width": 100, "height": 30}, receives_click=False)
    page = _Page(locator)

    with pytest.raises(WebSkrapError, match="covers the click point"):
        await _session().human_click(page, "#go")  # type: ignore[arg-type]

    # Checked before any pointer event, so the page saw nothing.
    assert page.mouse.moves == []
    assert page.mouse.clicks == []


@pytest.mark.asyncio
async def test_human_click_hit_tests_the_exact_click_point() -> None:
    locator = _Locator(box={"x": 10, "y": 200, "width": 100, "height": 30})
    page = _Page(locator)

    await _session().human_click(page, "#go", position={"x": 7, "y": 5})  # type: ignore[arg-type]

    assert locator.hit_tests == [[7, 5]]
