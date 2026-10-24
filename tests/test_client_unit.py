from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from anyio import CancelScope, sleep

from webskrap import client as client_module
from webskrap.cdp import CDPBrowser, CDPConnection, CDPPage
from webskrap.challenges import _CHALLENGE_SCRIPT
from webskrap.client import (
    _CONTENT_SCRIPT,
    _STATE_SCRIPT,
    LINKS_SCRIPT,
    WebSkrapClient,
    WebSkrapError,
    WebSkrapSession,
    browser_doctor,
    lavapipe_available,
    with_channel_fallback,
)
from webskrap.consent import SETTLED_PAGE_TIMEOUT_MS
from webskrap.errors import ErrorCode
from webskrap.human import bezier_path
from webskrap.models import SearchEngine, SessionConfig
from webskrap.profiles import get_profile

SEARCH_FIXTURES = Path(__file__).parent / "fixtures" / "search"
EXECUTABLE = Path("/browsers/chrome")


def _fake_browser(user_agent: str = "") -> MagicMock:
    browser = MagicMock(spec=CDPBrowser)
    browser.connection = MagicMock(spec=CDPConnection)
    browser.connection.send = AsyncMock(
        return_value={"userAgent": user_agent, "product": "Chrome/150.0", "cookies": []}
    )
    browser.close = AsyncMock()
    return browser


async def test_ua_probe_is_shared_cached_and_reset_on_close(monkeypatch) -> None:
    client = WebSkrapClient()
    probe = AsyncMock(return_value="Chrome/140.0")
    monkeypatch.setattr(client_module, "_probe_headless_user_agent", probe)
    config = SessionConfig(mask_headless_user_agent=True)
    uas = await asyncio.gather(
        *(client._headless_clean_user_agent(EXECUTABLE, config) for _ in range(8))
    )
    assert uas == ["Chrome/140.0"] * 8
    assert probe.await_count == 1
    await client._headless_clean_user_agent(Path("/browsers/chromium"), config)
    assert probe.await_count == 2
    await client.close()
    await client._headless_clean_user_agent(EXECUTABLE, config)
    assert probe.await_count == 3


async def test_failed_ua_probe_is_not_cached(monkeypatch) -> None:
    client = WebSkrapClient()
    probe = AsyncMock(side_effect=[None, "Chrome/140.0"])
    monkeypatch.setattr(client_module, "_probe_headless_user_agent", probe)
    assert await client._headless_clean_user_agent(EXECUTABLE, SessionConfig()) is None
    assert await client._headless_clean_user_agent(EXECUTABLE, SessionConfig()) == "Chrome/140.0"
    assert probe.await_count == 2


async def test_ua_probe_rewrites_native_version_and_closes_without_sleep(monkeypatch) -> None:
    browser = _fake_browser("Mozilla/5.0 HeadlessChrome/140.0.1 Safari/537.36")
    monkeypatch.setattr(client_module, "launch", AsyncMock(return_value=browser))
    sleep_mock = AsyncMock(side_effect=AssertionError("UA probe must not sleep"))
    monkeypatch.setattr("webskrap.client.asyncio.sleep", sleep_mock)
    ua = await WebSkrapClient()._headless_clean_user_agent(EXECUTABLE, SessionConfig())
    assert ua == "Mozilla/5.0 Chrome/140.0.1 Safari/537.36"
    browser.close.assert_awaited_once()
    sleep_mock.assert_not_awaited()


class _Launcher:
    """Stands in for ``cdp.launch`` and records how the browser was started."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, executable: Path, **options: Any) -> MagicMock:
        self.calls.append({"executable": executable, **options})
        if self.fail:
            raise WebSkrapError("browser failed to launch", code=ErrorCode.BROWSER_LAUNCH)
        return _fake_browser()

    @property
    def options(self) -> dict[str, Any]:
        return self.calls[-1] if self.calls else {}


class _FakeDisplay:
    def __init__(self) -> None:
        self.stopped = 0
        self.env = {"DISPLAY": ":99", "XAUTHORITY": "/tmp/webskrap-display-x/Xauthority"}

    async def stop(self) -> None:
        self.stopped += 1


def _launch_client(
    monkeypatch: pytest.MonkeyPatch, launcher: _Launcher, *, probe: bool = False
) -> tuple[WebSkrapClient, list[_FakeDisplay], list[tuple[int, int]]]:
    displays: list[_FakeDisplay] = []
    sizes: list[tuple[int, int]] = []

    async def fake_start(width: int, height: int) -> _FakeDisplay:
        sizes.append((width, height))
        displays.append(_FakeDisplay())
        return displays[-1]

    async def no_probe(*_args: object) -> str:
        raise AssertionError("the UA probe must not run here")

    monkeypatch.setattr("webskrap.client.VirtualDisplay.start", fake_start)
    monkeypatch.setattr(client_module, "launch", launcher)
    monkeypatch.setattr(client_module, "find_executable", lambda _channel: EXECUTABLE)
    client = WebSkrapClient()
    if not probe:
        monkeypatch.setattr(client, "_headless_clean_user_agent", no_probe)
    return client, displays, sizes


async def test_explicit_ua_skips_probe(monkeypatch) -> None:
    launcher = _Launcher()
    client, _displays, _sizes = _launch_client(monkeypatch, launcher)
    config = SessionConfig(mask_headless_user_agent=True, launch_args=["--user-agent=Caller UA"])
    session = await client._create_session("ua", config, get_profile(None))
    assert "--user-agent=Caller UA" in launcher.options["args"]
    await session.close()


async def test_masked_headless_ua_goes_in_as_a_launch_flag(monkeypatch) -> None:
    launcher = _Launcher()
    client, _displays, _sizes = _launch_client(monkeypatch, launcher, probe=True)
    monkeypatch.setattr(client, "_headless_clean_user_agent", AsyncMock(return_value="Clean UA"))
    session = await client._create_session(
        "ua", SessionConfig(mask_headless_user_agent=True), get_profile(None)
    )
    assert "--user-agent=Clean UA" in launcher.options["args"]
    assert launcher.options["headless"] is True
    await session.close()


async def test_session_close_survives_repeated_cancellation() -> None:
    async def checkpoint_close() -> None:
        await sleep(0)
        await sleep(0)

    browser = _fake_browser()
    browser.close = AsyncMock(side_effect=checkpoint_close)
    session = WebSkrapSession(
        name="cancelled", browser=browser, config=SessionConfig(), profile=get_profile(None)
    )
    with CancelScope() as scope:
        scope.cancel()
        await session.close()
    assert session._closed
    browser.close.assert_awaited_once()


class _ManagedSession:
    def __init__(self) -> None:
        self._closed = False

    async def close(self) -> None:
        self._closed = True


async def _new_managed_session() -> _ManagedSession:
    return _ManagedSession()


async def test_failed_close_keeps_client_ownership(monkeypatch) -> None:
    client = WebSkrapClient()
    monkeypatch.setattr(client, "_create_session", lambda *_args: _new_managed_session())
    session = await client.session("retained")
    close = session.close
    monkeypatch.setattr(session, "close", AsyncMock(side_effect=RuntimeError("close failed")))
    with pytest.raises(RuntimeError, match="close failed"):
        await client.close_session("retained")
    assert client._sessions["retained"] is session
    monkeypatch.setattr(session, "close", close)
    assert await client.close_session("retained") is True
    await client.close()


class _FakePage:
    """A CDP page stand-in that answers WebSkrap's own page scripts.

    ``challenges`` are the successive answers of the challenge probe; the
    last one repeats. ``states`` are what ``cdp_dom.element_state`` reports
    for a readiness target, in order.
    """

    def __init__(
        self,
        *,
        html: str = "<html><body>Page body</body></html>",
        text: str = "Visible body",
        challenges: list[object] | None = None,
    ) -> None:
        self.html = html
        self.text = text
        self.challenges = list(challenges or [None])
        self.requested: list[str] = []
        self.scripts: list[str] = []
        self.default_timeout_s = 30.0

    async def goto(self, url: str, *, wait_until: str, timeout_s: float) -> dict[str, Any]:
        self.requested.append(url)
        return {"status": 200, "headers": {"Content-Type": "text/html"}}

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.scripts.append(script)
        if script == _CHALLENGE_SCRIPT:
            answer = self.challenges.pop(0) if len(self.challenges) > 1 else self.challenges[0]
            if isinstance(answer, BaseException):
                raise answer
            return answer
        if script == _STATE_SCRIPT:
            return {"url": self.requested[-1], "title": "Title", "text": self.text}
        if script == _CONTENT_SCRIPT:
            return self.html
        if script == LINKS_SCRIPT:
            return {"links": [], "total": 0}
        raise AssertionError(f"unexpected script: {script[:60]}")


def _session(page: _FakePage | None = None, **config: Any) -> WebSkrapSession:
    session = WebSkrapSession(
        name="test",
        browser=_fake_browser(),
        config=SessionConfig(**config),
        profile=get_profile(None),
    )
    session.closed_pages = []  # type: ignore[attr-defined]

    async def new_page() -> Any:
        if page is None:
            raise AssertionError("no page may open")
        return page

    async def close_page(closed: Any) -> None:
        session.closed_pages.append(closed)  # type: ignore[attr-defined]

    session.new_page = new_page  # type: ignore[method-assign]
    session.close_page = close_page  # type: ignore[method-assign]
    return session


def _element_states(monkeypatch: pytest.MonkeyPatch, *states: str) -> list[str]:
    """Make ``cdp_dom.element_state`` report ``states`` in turn; return the targets asked."""
    asked: list[str] = []
    remaining = list(states)

    async def element_state(_page: object, target: str) -> str:
        asked.append(target)
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    monkeypatch.setattr("webskrap.client.cdp_dom.element_state", element_state)
    return asked


async def test_fetch_text_only_reads_visible_text_not_html() -> None:
    page = _FakePage()
    session = _session(page)

    result = await session.fetch("https://example.test", text_only=True)

    assert result.text == "Visible body"
    assert result.headers == {"content-type": "text/html"}
    assert _CONTENT_SCRIPT not in page.scripts
    assert session.closed_pages == [page]  # type: ignore[attr-defined]
    assert result.cookie_notice_declined is None


@pytest.mark.parametrize("provider", ["DataDome", "Cloudflare"])
async def test_fetch_blocks_challenges_before_consent_or_extraction(provider, monkeypatch) -> None:
    page = _FakePage(challenges=[provider])
    decline = AsyncMock()
    monkeypatch.setattr("webskrap.client._decline_cookies", decline)
    asked = _element_states(monkeypatch, "visible")
    session = _session(page, decline_cookies=True)
    with pytest.raises(WebSkrapError) as error:
        await session.fetch("https://example.test", ready_selector="#quote")
    assert error.value.code is ErrorCode.BLOCKED
    assert provider in str(error.value)
    decline.assert_not_awaited()
    assert asked == []
    assert _CONTENT_SCRIPT not in page.scripts
    assert session.closed_pages == [page]  # type: ignore[attr-defined]


async def test_fetch_waits_for_visible_selector_before_extracting(monkeypatch) -> None:
    page = _FakePage(text="Ready quote form")
    asked = _element_states(monkeypatch, "hidden", "hidden", "visible")
    session = _session(page)
    result = await session.fetch(
        "https://example.test", ready_selector="#quote", timeout_ms=4321, text_only=True
    )
    assert asked == ["#quote"] * 3
    assert result.text == "Ready quote form"
    assert session.closed_pages == [page]  # type: ignore[attr-defined]


@pytest.mark.parametrize("provider", [None, "DataDome"])
async def test_readiness_timeout_rechecks_challenge_and_closes_page(provider, monkeypatch) -> None:
    page = _FakePage(challenges=[None, provider])
    _element_states(monkeypatch, "hidden")
    session = _session(page)
    with pytest.raises(WebSkrapError) as error:
        await session.fetch("https://example.test", ready_selector="#quote", timeout_ms=150)
    assert error.value.code is (ErrorCode.BLOCKED if provider else ErrorCode.TIMEOUT)
    assert _CONTENT_SCRIPT not in page.scripts
    assert session.closed_pages == [page]  # type: ignore[attr-defined]


async def test_fetch_rechecks_challenge_after_consent(monkeypatch) -> None:
    page = _FakePage(challenges=[None, "DataDome"])
    monkeypatch.setattr("webskrap.client._decline_cookies", AsyncMock(return_value="cmp"))
    session = _session(page, decline_cookies=True)
    with pytest.raises(WebSkrapError) as error:
        await session.fetch("https://example.test")
    assert error.value.code is ErrorCode.BLOCKED
    assert session.closed_pages == [page]  # type: ignore[attr-defined]


@pytest.mark.parametrize("selector", ["", "  "])
async def test_fetch_rejects_blank_readiness_without_opening_page(selector) -> None:
    with pytest.raises(WebSkrapError) as error:
        await _session().fetch("https://example.test", ready_selector=selector)
    assert error.value.code is ErrorCode.USAGE


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("file:///etc/passwd", id="file"),
        pytest.param("ftp://example.test/x", id="ftp"),
        pytest.param("https://user:pass@example.test/", id="userinfo"),
    ],
)
async def test_fetch_rejects_unfetchable_targets_without_a_page(url: str) -> None:
    with pytest.raises(WebSkrapError) as caught:
        await _session().fetch(url)
    assert caught.value.code is ErrorCode.USAGE


def _record_declines(monkeypatch: pytest.MonkeyPatch, answer: str | None) -> list[float]:
    timeouts: list[float] = []

    async def fake_decline(_page: object, *, timeout_ms: float) -> str | None:
        timeouts.append(timeout_ms)
        return answer

    monkeypatch.setattr("webskrap.client._decline_cookies", fake_decline)
    return timeouts


async def test_fetch_declines_cookie_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts = _record_declines(monkeypatch, "cmp")
    session = _session(_FakePage(), decline_cookies=True, decline_cookies_timeout_ms=1234)

    result = await session.fetch("https://example.test")

    assert result.cookie_notice_declined == "cmp"
    assert timeouts == [1234]


async def test_networkidle_shrinks_the_decline_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts = _record_declines(monkeypatch, None)
    session = _session(_FakePage(), decline_cookies=True, decline_cookies_timeout_ms=5_000)

    await session.fetch("https://example.test", wait_until="networkidle")
    await session.fetch("https://example.test", wait_until="domcontentloaded")

    # networkidle already waited out the CMP script; do not wait for it twice.
    assert timeouts == [SETTLED_PAGE_TIMEOUT_MS, 5_000]


async def test_fetch_skips_cookie_decline_when_disabled(monkeypatch) -> None:
    timeouts = _record_declines(monkeypatch, "cmp")

    result = await _session(_FakePage(), decline_cookies=False).fetch("https://example.test")

    assert result.cookie_notice_declined is None
    assert timeouts == []


async def test_fetch_consent_override_is_per_call_and_preserves_config(monkeypatch) -> None:
    timeouts = _record_declines(monkeypatch, "cmp")
    session = _session(_FakePage(), decline_cookies=True)
    skipped = await session.fetch("https://example.test", decline_cookies=False)
    clicked = await session.fetch("https://example.test", decline_cookies=True)
    assert skipped.cookie_notice_declined is None
    assert clicked.cookie_notice_declined == "cmp"
    assert len(timeouts) == 1
    assert session.config.decline_cookies is True


def _search_page(fixture: str) -> _FakePage:
    return _FakePage(html=(SEARCH_FIXTURES / fixture).read_text(encoding="utf-8"))


async def test_search_loads_the_results_page_and_caps_the_hits(monkeypatch) -> None:
    page = _search_page("bing.html")
    # Bing's head-only interstitial has no body children; the read waits them out.
    asked = _element_states(monkeypatch, "detached", "visible")
    session = _session(page)

    result = await session.search("example domain", max_results=2)

    assert page.requested == ["https://www.bing.com/search?q=example+domain"]
    assert asked == ["body > *", "body > *"]
    assert session.closed_pages == [page]  # type: ignore[attr-defined]
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


async def test_search_selects_the_engine(monkeypatch) -> None:
    page = _search_page("ddg.html")
    _element_states(monkeypatch, "visible")

    result = await _session(page).search("example domain", engine=SearchEngine.DDG)

    assert page.requested == ["https://html.duckduckgo.com/html/?q=example+domain"]
    assert result.engine is SearchEngine.DDG
    assert result.hits_total == 4


async def test_search_goes_through_the_consent_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _record_declines(monkeypatch, "cmp")
    _element_states(monkeypatch, "visible")
    session = _session(_search_page("bing.html"), decline_cookies=True)

    result = await session.search("example domain", engine=SearchEngine.BING)

    assert result.cookie_notice_declined == "cmp"


async def test_search_reports_a_challenge_page_as_blocked(monkeypatch) -> None:
    _element_states(monkeypatch, "visible")
    with pytest.raises(WebSkrapError) as excinfo:
        await _session(_search_page("bing_challenge.html")).search("example domain")

    assert excinfo.value.code is ErrorCode.BLOCKED


async def test_search_rejects_a_blank_query_before_opening_a_page() -> None:
    with pytest.raises(WebSkrapError) as excinfo:
        await _session().search("   ")

    assert excinfo.value.code is ErrorCode.USAGE


async def test_search_on_a_closed_session_fails() -> None:
    session = _session()
    session._closed = True

    with pytest.raises(WebSkrapError, match="is closed"):
        await session.search("example domain")


async def test_client_search_uses_a_throwaway_session(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _search_page("bing.html")
    _element_states(monkeypatch, "visible")
    session = _session(page)
    closed: list[str] = []

    async def fake_close() -> None:
        closed.append(session.name)
        session._closed = True

    monkeypatch.setattr(session, "close", fake_close)
    client = WebSkrapClient()
    monkeypatch.setattr(client, "_create_session", lambda *_args: _resolved(session))

    result = await client.search("example domain", max_results=1)
    await client.close()

    assert [hit.url for hit in result.hits] == ["https://example.com/"]
    assert result.hits_total == 3
    assert page.requested == ["https://www.bing.com/search?q=example+domain"]
    assert closed == [session.name]
    assert client._sessions == {}


async def _resolved(session: WebSkrapSession) -> WebSkrapSession:
    return session


async def test_human_click_rejects_a_bad_click_count() -> None:
    with pytest.raises(WebSkrapError, match="click_count") as excinfo:
        await _session().human_click(MagicMock(spec=CDPPage), "#go", click_count=3)
    assert excinfo.value.code is ErrorCode.USAGE


@pytest.mark.asyncio
async def test_closed_named_session_is_recreated(monkeypatch: pytest.MonkeyPatch) -> None:
    client = WebSkrapClient()
    monkeypatch.setattr(client, "_create_session", lambda *_args: _new_managed_session())

    first = await client.session("reopen")
    await first.close()
    second = await client.session("reopen")
    await client.close()

    assert second is not first


@pytest.mark.asyncio
async def test_concurrent_named_session_is_created_once(monkeypatch: pytest.MonkeyPatch) -> None:
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


async def test_virtual_display_session_runs_headed_on_the_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _Launcher()
    client, displays, sizes = _launch_client(monkeypatch, launcher)
    config = SessionConfig(virtual_display=True, mask_headless_user_agent=True)

    session = await client._create_session("vd", config, get_profile("desktop-chrome"))

    env = launcher.options["env"]
    assert env["DISPLAY"] == ":99"
    assert env["XAUTHORITY"].endswith("Xauthority")
    assert "PATH" in env  # the rest of the environment survives
    assert launcher.options["headless"] is False
    assert not any(str(a).startswith("--user-agent") for a in launcher.options["args"])
    assert sizes == [(1920, 1080)]

    await session.close()
    assert displays[0].stopped == 1


async def test_virtual_display_is_stopped_when_launch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, displays, _sizes = _launch_client(monkeypatch, _Launcher(fail=True))

    with pytest.raises(WebSkrapError, match="failed to launch"):
        await client._create_session(
            "vd", SessionConfig(virtual_display=True), get_profile("desktop-chrome")
        )

    assert displays[0].stopped == 1


async def test_virtual_display_keeps_the_native_profile_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    launcher = _Launcher()
    client, _displays, _sizes = _launch_client(monkeypatch, launcher)
    config = SessionConfig(virtual_display=True, profile_mode="locale")

    session = await client._create_session("vd", config, get_profile("desktop-chrome"))
    await session.close()

    env = launcher.options["env"]
    assert env["DISPLAY"] == ":99"
    assert env["TZ"] == "Europe/Paris"
    assert env["LANG"] == "en_US.UTF-8"


async def test_unknown_profile_timezone_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    launcher = _Launcher()
    client, _displays, _sizes = _launch_client(monkeypatch, launcher)
    profile = get_profile("desktop-chrome").model_copy(update={"timezone_id": "Mars/Olympus"})

    with pytest.raises(WebSkrapError, match="unknown timezone") as excinfo:
        await client._create_session("tz", SessionConfig(profile_mode="locale"), profile)

    assert excinfo.value.code is ErrorCode.USAGE
    assert launcher.calls == []


async def test_mesa_gpu_without_lavapipe_fails_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _Launcher()
    client, displays, _sizes = _launch_client(monkeypatch, launcher)
    monkeypatch.setattr("webskrap.client.lavapipe_available", lambda: False)
    config = SessionConfig(gpu="mesa", virtual_display=True)

    with pytest.raises(WebSkrapError, match="mesa-vulkan-drivers") as excinfo:
        await client._create_session("gpu", config, get_profile("desktop-chrome"))

    assert excinfo.value.code is ErrorCode.BROWSER_LAUNCH
    assert launcher.calls == []  # the browser was never launched
    assert displays == []  # nor the display


async def test_storage_state_is_restored_once_the_browser_runs(monkeypatch) -> None:
    launcher = _Launcher()
    client, _displays, _sizes = _launch_client(monkeypatch, launcher)
    restored: list[object] = []

    async def apply(browser: object, state: object) -> None:
        restored.append(state)

    monkeypatch.setattr("webskrap.client.cdp_setup.apply_storage_state", apply)
    state = {"cookies": [], "origins": []}
    session = await client._create_session(
        "state", SessionConfig(storage_state=state), get_profile(None)
    )
    await session.close()
    assert restored == [state]


async def test_doctor_hints_at_the_sandbox_when_that_is_what_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launches: list[dict[str, Any]] = []

    async def fake_launch(executable: Path, **options: Any) -> None:
        launches.append({"executable": executable, **options})
        raise WebSkrapError(
            "browser exited during startup\nChromium sandboxing failed!",
            code=ErrorCode.BROWSER_LAUNCH,
        )

    monkeypatch.setattr(client_module, "find_executable", lambda channel: Path(f"/b/{channel}"))
    monkeypatch.setattr(client_module, "launch", fake_launch)

    report = await browser_doctor(chromium_sandbox=True)

    assert report["ok"] is False
    assert report["browser_identity"] is None
    assert all("--no-sandbox" not in options["args"] for options in launches)
    assert [options["executable"] for options in launches] == [Path("/b/chrome")]
    assert "--no-sandbox" in str(report["hint"])


async def test_doctor_tries_edge_before_bundled_chromium(monkeypatch: pytest.MonkeyPatch) -> None:
    channels: list[str | None] = []

    def fake_find(channel: str | None) -> Path:
        channels.append(channel)
        if channel == "chrome":
            raise WebSkrapError(
                "Chromium distribution 'chrome' is not found at /opt",
                code=ErrorCode.BROWSER_LAUNCH,
            )
        return Path(f"/b/{channel}")

    monkeypatch.setattr(client_module, "find_executable", fake_find)
    monkeypatch.setattr(client_module, "launch", AsyncMock(return_value=_fake_browser()))

    report = await browser_doctor()

    assert report["ok"] is True
    assert report["channel"] == "msedge"
    assert report["browser_identity"] == "Edge"
    assert report["browser_version"] == "Chrome/150.0"
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


def _launcher(missing: set[str | None], attempts: list[str | None]):
    async def run(config: SessionConfig) -> str | None:
        attempts.append(config.channel)
        if config.channel in missing:
            raise RuntimeError(f"Chromium distribution '{config.channel}' is not found at /opt/x")
        return config.channel

    return run


async def test_channel_fallback_remembers_the_channel_that_launched() -> None:
    attempts: list[str | None] = []
    run = _launcher({"chrome", "msedge"}, attempts)
    retries: list[tuple[str | None, str | None]] = []

    first = await with_channel_fallback(
        run, SessionConfig(channel="chrome"), on_retry=lambda *pair: retries.append(pair)
    )
    second = await with_channel_fallback(run, SessionConfig(channel="chrome"))

    assert (first, second) == ("chromium", "chromium")
    assert attempts == ["chrome", "msedge", "chromium", "chromium"]
    assert retries == [("chrome", "msedge"), ("msedge", "chromium")]


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("BrowserType.launch: No usable sandbox!"),
        RuntimeError("net::ERR_NAME_NOT_RESOLVED"),
    ],
)
async def test_channel_fallback_does_not_retry_sandbox_or_page_failures(error) -> None:
    attempts: list[str | None] = []

    async def run(config: SessionConfig) -> None:
        attempts.append(config.channel)
        raise error

    with pytest.raises(RuntimeError) as raised:
        await with_channel_fallback(run, SessionConfig(channel="chrome"))
    assert raised.value is error
    assert attempts == ["chrome"]


async def test_channel_fallback_raises_the_last_launch_failure() -> None:
    attempts: list[str | None] = []
    with pytest.raises(RuntimeError, match="'chromium' is not found"):
        await with_channel_fallback(
            _launcher({"chrome", "msedge", "chromium"}, attempts), SessionConfig(channel="chrome")
        )
    assert attempts == ["chrome", "msedge", "chromium"]


@pytest.mark.parametrize("exclusive", [False, True])
async def test_channel_fallback_lets_calls_overlap_after_resolution(exclusive) -> None:
    gates = [asyncio.Event() for _ in range(3)]
    started: list[int] = []
    active: list[int] = []

    async def run(config: SessionConfig) -> None:
        if config.channel == "chrome":
            raise RuntimeError("Chromium distribution 'chrome' is not found at /opt/x")
        gate = gates[len(started)]
        started.append(1)
        active.append(1)
        await gate.wait()
        active.pop()

    async def settle() -> None:
        for _ in range(10):
            await sleep(0)

    calls = [
        asyncio.ensure_future(
            with_channel_fallback(run, SessionConfig(channel="chrome"), exclusive=exclusive)
        )
        for _ in range(3)
    ]
    await settle()
    # Exclusive cold-start callers wait for the first; others never wait.
    assert len(active) == (1 if exclusive else 3)
    if exclusive:
        gates[0].set()
        await settle()
        # Once the channel is known, queued callers run together.
        assert len(active) == 2
    for gate in gates:
        gate.set()
    await asyncio.gather(*calls)


async def test_channel_fallback_remembers_a_channel_whose_page_failed() -> None:
    attempts: list[str | None] = []

    async def run(config: SessionConfig) -> None:
        attempts.append(config.channel)
        if config.channel == "chrome":
            raise RuntimeError("Chromium distribution 'chrome' is not found at /opt/x")
        raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

    for _ in range(2):
        with pytest.raises(RuntimeError, match="ERR_NAME_NOT_RESOLVED"):
            await with_channel_fallback(run, SessionConfig(channel="chrome"), exclusive=True)
    assert attempts == ["chrome", "msedge", "msedge"]
