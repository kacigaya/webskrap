from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from webskrap import diagnostics, mcp_server
from webskrap.client import WebSkrapError
from webskrap.errors import RECOVERY_HINTS, ErrorCode
from webskrap.models import FetchResult, Link
from webskrap.paths import MCP_PROFILE_DIR_ENV, OUTPUT_DIR_ENV
from webskrap.profiles import list_profiles


class _FakeClient:
    calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        return None

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append({"url": url, **kwargs})
        text = "Readable body" if kwargs.get("text_only") else "<html>abcdef</html>"
        return FetchResult(
            url=url,
            final_url=f"{url}/final",
            status=200,
            ok=True,
            headers={"content-type": "text/html"},
            text=text,
            title="Example",
            cookies=[],
            timings={"elapsed_ms": 12.34},
        )


def _fake_client(monkeypatch: Any) -> None:
    _FakeClient.calls = []
    monkeypatch.setattr(mcp_server, "WebSkrapClient", _FakeClient)


def test_fetch_defaults_to_clean_text(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = asyncio.run(mcp_server.fetch("https://example.test"))

    assert _FakeClient.calls[0]["text_only"] is True
    assert result["text"] == "Readable body"


def test_fetch_uses_stealth_driver_by_default(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    asyncio.run(mcp_server.fetch("https://example.test"))

    config = _FakeClient.calls[0]["config"]
    assert config.driver == "patchright"
    assert config.channel == "chrome"
    assert config.headless is True
    assert _FakeClient.calls[0]["wait_until"] == "networkidle"


def test_fetch_text_only_false_returns_html(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = asyncio.run(mcp_server.fetch("https://example.test", text_only=False))

    assert _FakeClient.calls[0]["text_only"] is False
    assert result["text"] == "<html>abcdef</html>"


def test_stealth_fetch_defaults_to_clean_text(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = asyncio.run(mcp_server.stealth_fetch("https://example.test"))

    assert _FakeClient.calls[0]["text_only"] is True
    assert result["text"] == "Readable body"


def test_stealth_fetch_confines_persistent_profile(monkeypatch: Any, tmp_path: Path) -> None:
    _fake_client(monkeypatch)
    root = tmp_path / "profiles"
    monkeypatch.setenv(MCP_PROFILE_DIR_ENV, str(root))

    asyncio.run(mcp_server.stealth_fetch("https://example.test", user_data_dir="accounts/shop"))

    config = _FakeClient.calls[0]["config"]
    assert config.user_data_dir == root / "accounts" / "shop"
    assert config.user_data_dir.is_dir()


@pytest.mark.parametrize("path", [".", "..", "../escape", "/tmp/escape"])
def test_stealth_fetch_rejects_profile_paths_outside_root(
    monkeypatch: Any, tmp_path: Path, path: str
) -> None:
    _fake_client(monkeypatch)
    monkeypatch.setenv(MCP_PROFILE_DIR_ENV, str(tmp_path / "profiles"))

    with pytest.raises(WebSkrapError, match="profile path must"):
        asyncio.run(mcp_server.stealth_fetch("https://example.test", user_data_dir=path))

    assert _FakeClient.calls == []


def test_stealth_fetch_rejects_profile_symlink_outside_root(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _fake_client(monkeypatch)
    root = tmp_path / "profiles"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv(MCP_PROFILE_DIR_ENV, str(root))

    with pytest.raises(WebSkrapError, match="profile path must stay inside"):
        asyncio.run(
            mcp_server.stealth_fetch(
                "https://example.test",
                user_data_dir="escape/account",
            )
        )

    assert _FakeClient.calls == []


def test_browser_tools_are_registered() -> None:
    names = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}

    assert {
        "browser_open",
        "browser_goto",
        "browser_snapshot",
        "browser_interact",
        "browser_wait_for",
        "browser_press",
        "browser_screenshot",
        "browser_eval",
        "browser_close",
        "browser_list",
    } <= names


def test_browser_interact_rejects_unknown_action() -> None:
    with pytest.raises(WebSkrapError, match="unknown action"):
        asyncio.run(mcp_server.browser_interact("explode", "e1"))


def test_browser_interact_validates_value_arity() -> None:
    with pytest.raises(WebSkrapError, match="exactly one value"):
        asyncio.run(mcp_server.browser_interact("fill", "e1"))
    with pytest.raises(WebSkrapError, match="no value"):
        asyncio.run(mcp_server.browser_interact("click", "e1", values=["x"]))


def test_browser_action_without_open_session(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path))

    with pytest.raises(WebSkrapError, match="not open"):
        asyncio.run(mcp_server.browser_goto("https://example.test"))


def test_browser_list_empty(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path))

    assert asyncio.run(mcp_server.browser_list()) == {"sessions": []}


def test_browser_screenshot_generates_a_name_in_the_output_root(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv(OUTPUT_DIR_ENV, str(tmp_path / "out"))

    # No session is open, so the tool fails after the path has been accepted:
    # the directory is created, which is what this asserts.
    with pytest.raises(WebSkrapError, match="not open"):
        asyncio.run(mcp_server.browser_screenshot())

    assert (tmp_path / "out").is_dir()


def test_browser_screenshot_creates_nested_directories(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv(OUTPUT_DIR_ENV, str(tmp_path / "out"))

    with pytest.raises(WebSkrapError, match="not open"):
        asyncio.run(mcp_server.browser_screenshot(path="runs/today/shot.png"))

    assert (tmp_path / "out" / "runs" / "today").is_dir()


@pytest.mark.parametrize(
    ("path", "message"),
    [
        pytest.param("../escape.png", "escapes it", id="parent"),
        pytest.param("runs/../../escape.png", "escapes it", id="normalized-traversal"),
        pytest.param("/etc/escape.png", "is absolute", id="absolute"),
        pytest.param("/tmp/escape.png", "is absolute", id="absolute-tmp"),
    ],
)
def test_browser_screenshot_rejects_paths_outside_the_output_root(
    monkeypatch: Any, tmp_path: Path, path: str, message: str
) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv(OUTPUT_DIR_ENV, str(tmp_path / "out"))

    with pytest.raises(WebSkrapError, match=message):
        asyncio.run(mcp_server.browser_screenshot(path=path))

    assert not (tmp_path / "escape.png").exists()


@pytest.mark.parametrize("session", [".", ".."])
def test_browser_close_delete_data_rejects_traversal(
    monkeypatch: Any, tmp_path: Path, session: str
) -> None:
    browser_root = tmp_path / "browser"
    browser_root.mkdir()
    marker = tmp_path / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(browser_root))

    with pytest.raises(WebSkrapError, match="invalid session name"):
        asyncio.run(mcp_server.browser_close(session=session, delete_data=True))

    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.browser
def test_browser_mcp_lifecycle(monkeypatch: Any, persistent_session_env: Path) -> None:
    tmp_path = persistent_session_env
    monkeypatch.setenv(OUTPUT_DIR_ENV, str(tmp_path / "out"))
    page = (
        "data:text/html,<title>mcp-test</title>"
        "<button onclick=\"this.textContent='clicked'\">Press me</button>"
    )

    opened = asyncio.run(mcp_server.browser_open())
    assert opened["reused"] is False
    try:
        goto = asyncio.run(mcp_server.browser_goto(page))
        assert goto["title"] == "mcp-test"

        snapshot = asyncio.run(mcp_server.browser_snapshot())
        assert snapshot["snapshot_truncated"] is False
        ref = snapshot["snapshot"].split('button "Press me" [ref=')[1].split("]")[0]

        short = asyncio.run(mcp_server.browser_snapshot(max_chars=10))
        assert short["snapshot_truncated"] is True
        assert len(short["snapshot"]) == 10
        # The clipped tail is reachable rather than lost.
        rest = asyncio.run(mcp_server.browser_snapshot(offset=short["next_snapshot_offset"]))
        assert short["snapshot"] + rest["snapshot"] == snapshot["snapshot"]
        assert rest["next_snapshot_offset"] is None

        clicked = asyncio.run(mcp_server.browser_interact("click", ref))
        assert clicked["title"] == "mcp-test"

        evaluated = asyncio.run(
            mcp_server.browser_eval("document.querySelector('button').textContent")
        )
        assert evaluated["result"] == "clicked"
        assert evaluated["result_truncated"] is False

        # Screenshots are confined to the output root, so ask for a relative
        # name and assert it landed inside it.
        shot = asyncio.run(mcp_server.browser_screenshot(path="runs/shot.png"))
        assert shot["path"] == str(tmp_path / "out" / "runs" / "shot.png")
        assert Path(shot["path"]).stat().st_size > 0

        listed = asyncio.run(mcp_server.browser_list())
        assert listed["sessions"][0]["running"] is True
    finally:
        closed = asyncio.run(mcp_server.browser_close())

    assert closed["closed"] == [{"session": "default", "deleted_data": False}]
    with pytest.raises(WebSkrapError, match="not open"):
        asyncio.run(mcp_server.browser_snapshot())


def test_fetch_pages_through_text_with_offset(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    first = asyncio.run(mcp_server.fetch("https://example.test", max_chars=5))
    assert first["text"] == "Reada"
    assert first["next_text_offset"] == 5

    rest = asyncio.run(
        mcp_server.fetch("https://example.test", max_chars=5, offset=first["next_text_offset"])
    )
    assert rest["text"] == "ble b"


def test_stealth_fetch_accepts_an_offset(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = asyncio.run(mcp_server.stealth_fetch("https://example.test", max_chars=4, offset=9))

    assert result["text"] == "body"
    assert result["text_offset"] == 9
    assert result["next_text_offset"] is None


def test_fetch_does_not_collect_links_unless_asked(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    result = asyncio.run(mcp_server.fetch("https://example.test"))

    assert _FakeClient.calls[0]["include_links"] is False
    assert result["links"] == []
    assert result["links_truncated"] is False


def test_fetch_forwards_the_link_budget(monkeypatch: Any) -> None:
    _fake_client(monkeypatch)

    asyncio.run(mcp_server.fetch("https://example.test", include_links=True, max_links=5))

    assert _FakeClient.calls[0]["include_links"] is True
    assert _FakeClient.calls[0]["max_links"] == 5


def test_shaped_links_report_what_the_cap_dropped(monkeypatch: Any) -> None:
    class _LinkClient(_FakeClient):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            result = await super().fetch(url, **kwargs)
            return result.model_copy(
                update={"links": [Link(href="https://a.test", text="A")], "links_total": 4}
            )

    monkeypatch.setattr(mcp_server, "WebSkrapClient", _LinkClient)

    result = asyncio.run(mcp_server.fetch("https://example.test", include_links=True))

    assert result["links"] == [{"href": "https://a.test", "text": "A"}]
    assert result["links_total"] == 4
    assert result["links_truncated"] is True


def test_browser_wait_for_requires_exactly_one_condition() -> None:
    with pytest.raises(WebSkrapError, match="exactly one of"):
        asyncio.run(mcp_server.browser_wait_for())
    with pytest.raises(WebSkrapError, match="exactly one of"):
        asyncio.run(mcp_server.browser_wait_for(text="a", selector="b"))


def test_browser_wait_for_rejects_a_load_state_that_cannot_be_awaited() -> None:
    with pytest.raises(WebSkrapError, match="load_state must be one of") as caught:
        asyncio.run(mcp_server.browser_wait_for(load_state="commit"))

    assert caught.value.code is ErrorCode.USAGE


@pytest.mark.browser
def test_browser_wait_for_deferred_text(persistent_session_env: Path) -> None:
    page = (
        "data:text/html,<title>deferred</title><p id=status>Loading</p>"
        "<script>setTimeout(() => {"
        "document.getElementById('status').textContent = 'Ready';"
        "}, 400)</script>"
    )
    asyncio.run(mcp_server.browser_open(page))
    try:
        waited = asyncio.run(mcp_server.browser_wait_for(text="Ready"))
        assert waited["matched"] == "text"
        assert waited["title"] == "deferred"

        gone = asyncio.run(mcp_server.browser_wait_for(text_gone="Loading"))
        assert gone["matched"] == "text_gone"

        idle = asyncio.run(mcp_server.browser_wait_for(load_state="networkidle"))
        assert idle["matched"] == "load_state"
    finally:
        asyncio.run(mcp_server.browser_close())


@pytest.mark.browser
def test_browser_eval_bounds_a_large_result(persistent_session_env: Path) -> None:
    asyncio.run(mcp_server.browser_open("data:text/html,<title>big</title>"))
    try:
        bounded = asyncio.run(mcp_server.browser_eval("'x'.repeat(500)", max_chars=20))

        assert bounded["result"] is None
        assert bounded["result_truncated"] is True
        assert bounded["result_length"] == 502
        assert len(bounded["result_json"]) == 20
    finally:
        asyncio.run(mcp_server.browser_close())


def test_doctor_reports_configuration(monkeypatch: Any, tmp_path: Path) -> None:
    async def fake_doctor() -> dict[str, Any]:
        return {"ok": True, "message": "ready", "channel": "chrome"}

    monkeypatch.setattr(diagnostics, "browser_doctor", fake_doctor)
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path))

    report = asyncio.run(mcp_server.doctor())

    assert report["ok"] is True
    assert report["versions"]["webskrap"]
    assert report["paths"]["sessions_root"] == str(tmp_path)
    assert report["sessions"] == []


def test_server_instructions_carry_the_tool_choice_rules() -> None:
    instructions = mcp_server.mcp.instructions or ""

    # The rule an agent most needs before its first call, and the two limits it
    # otherwise discovers by wasting a call.
    assert "Prefer it over fetch" in instructions
    assert "no web search tool" in instructions
    assert "one page per session" in instructions
    assert "next_text_offset" in instructions


def test_server_instructions_stay_short_enough_to_ship_in_a_system_prompt() -> None:
    # These are prepended to a client's context on every session, so they are
    # budgeted rather than allowed to grow into a manual. SKILL.md is where the
    # long form lives.
    assert len(mcp_server.mcp.instructions or "") < 3_000


def test_every_tool_declares_a_title_and_annotations() -> None:
    tools = asyncio.run(mcp_server.mcp.list_tools())

    assert tools
    for tool in tools:
        assert tool.title, tool.name
        assert tool.annotations is not None, tool.name


def test_read_only_tools_are_marked_read_only() -> None:
    annotations = {tool.name: tool.annotations for tool in asyncio.run(mcp_server.mcp.list_tools())}

    for name in ("fetch", "stealth_fetch", "doctor", "browser_snapshot", "browser_list"):
        assert annotations[name].readOnlyHint is True, name
    for name in ("browser_interact", "browser_eval", "browser_close"):
        assert annotations[name].readOnlyHint is False, name


def test_tools_that_can_lose_data_or_submit_forms_are_marked_destructive() -> None:
    annotations = {tool.name: tool.annotations for tool in asyncio.run(mcp_server.mcp.list_tools())}

    # browser_close --delete_data throws away cookies and logins; a click or an
    # Enter can submit a form on a site WebSkrap does not own.
    assert annotations["browser_close"].destructiveHint is True
    assert annotations["browser_interact"].destructiveHint is True
    assert annotations["browser_press"].destructiveHint is True
    # Listing sessions must not look like closing one.
    assert annotations["browser_list"].destructiveHint is False


def test_tools_that_reach_the_open_web_say_so() -> None:
    annotations = {tool.name: tool.annotations for tool in asyncio.run(mcp_server.mcp.list_tools())}

    for name in ("fetch", "stealth_fetch", "browser_goto", "browser_interact"):
        assert annotations[name].openWorldHint is True, name
    for name in ("doctor", "browser_list", "browser_snapshot"):
        assert annotations[name].openWorldHint is False, name


def test_tool_failures_carry_their_code_and_hint(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path))

    with pytest.raises(WebSkrapError) as caught:
        asyncio.run(mcp_server.browser_snapshot())

    message = str(caught.value)
    assert "is not open" in message
    assert "[code: no_session]" in message
    assert RECOVERY_HINTS[ErrorCode.NO_SESSION] in message
    assert caught.value.code is ErrorCode.NO_SESSION


def test_a_guarded_failure_is_not_annotated_twice(monkeypatch: Any, tmp_path: Path) -> None:
    # browser_open guards the launch and then runs a page action through
    # _browser_action, which guards again; the hint must appear once.
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path))

    with pytest.raises(WebSkrapError) as caught:
        asyncio.run(mcp_server.browser_close(session="../escape"))

    assert str(caught.value).count("[code:") == 1


def test_fetch_failures_are_classified(monkeypatch: Any) -> None:
    class _BrokenClient(_FakeClient):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED at https://nope.test")

    monkeypatch.setattr(mcp_server, "WebSkrapClient", _BrokenClient)

    with pytest.raises(WebSkrapError) as caught:
        asyncio.run(mcp_server.fetch("https://nope.test"))

    assert caught.value.code is ErrorCode.NAVIGATION
    assert "[code: navigation]" in str(caught.value)


def test_a_playwright_call_log_is_reduced_to_its_first_line(monkeypatch: Any) -> None:
    class _ChattyClient(_FakeClient):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            raise RuntimeError("Timeout 30000ms exceeded\nCall log:\n  - navigating\n  - waiting")

    monkeypatch.setattr(mcp_server, "WebSkrapClient", _ChattyClient)

    with pytest.raises(WebSkrapError) as caught:
        asyncio.run(mcp_server.fetch("https://slow.test"))

    assert "Call log" not in str(caught.value)
    assert caught.value.code is ErrorCode.TIMEOUT


def test_resources_are_registered() -> None:
    resources = {str(resource.uri) for resource in asyncio.run(mcp_server.mcp.list_resources())}

    assert resources == {"webskrap://guide", "webskrap://profiles", "webskrap://sessions"}


def test_profiles_resource_lists_the_names_the_fetch_tools_accept() -> None:
    payload = json.loads(mcp_server.profiles_resource())

    names = {profile["name"] for profile in payload["profiles"]}
    assert "desktop-chrome" in names
    assert names == {profile.name for profile in list_profiles()}


def test_sessions_resource_matches_the_browser_list_tool(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("WEBSKRAP_BROWSER_DIR", str(tmp_path))
    (tmp_path / "shop" / "user-data").mkdir(parents=True)

    assert json.loads(mcp_server.sessions_resource()) == asyncio.run(mcp_server.browser_list())


def test_guide_documents_every_registered_tool() -> None:
    guide = mcp_server.guide_resource()

    # The guide is the long form the short instructions point at, so a tool
    # that never reaches it is a tool an agent will not find.
    for tool in asyncio.run(mcp_server.mcp.list_tools()):
        assert f"`{tool.name}`" in guide, tool.name


def test_guide_documents_every_error_code() -> None:
    guide = mcp_server.guide_resource()

    for code in ErrorCode:
        if code is ErrorCode.INTERNAL:
            continue
        assert f"`{code}`" in guide, code
