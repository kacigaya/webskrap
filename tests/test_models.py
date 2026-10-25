from __future__ import annotations

import sys
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from webskrap import (
    BrowserProfile,
    FetchResult,
    GpuBackend,
    ProxyConfig,
    SearchEngine,
    SearchHit,
    SearchResult,
    SessionConfig,
    Viewport,
    WebRtcIPHandlingPolicy,
)
from webskrap.models import (
    TextWindow,
    shape_fetch_result,
    shape_search_result,
    text_window,
    user_agent_metadata,
)


def test_full_profile_mode_emulates_the_whole_profile() -> None:
    profile = BrowserProfile(
        name="test",
        user_agent="Custom/1.0",
        viewport=Viewport(width=1280, height=720),
        locale="en-US",
        timezone_id="Europe/Paris",
        navigator_languages=["en-US", "en"],
    )

    commands = dict(SessionConfig(profile_mode="full").emulation_commands(profile))

    metrics = commands["Emulation.setDeviceMetricsOverride"]
    assert (metrics["width"], metrics["height"]) == (1280, 720)
    assert commands["Emulation.setUserAgentOverride"] == {
        "userAgent": "Custom/1.0",
        "acceptLanguage": "en-US,en;q=0.9",
    }
    assert commands["Emulation.setLocaleOverride"] == {"locale": "en-US"}
    assert commands["Emulation.setTimezoneOverride"] == {"timezoneId": "Europe/Paris"}
    headers = commands["Network.setExtraHTTPHeaders"]["headers"]
    assert headers["Accept-Language"] == "en-US,en;q=0.9"
    assert "Accept" not in headers


def test_full_profile_sends_user_agent_metadata_with_the_override() -> None:
    profile = BrowserProfile(name="test", user_agent="Custom/1.0")
    metadata = {"platform": "Linux"}

    commands = dict(
        SessionConfig(profile_mode="full").emulation_commands(profile, user_agent_metadata=metadata)
    )

    assert commands["Emulation.setUserAgentOverride"]["userAgentMetadata"] == metadata


WINDOWS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/{}.0.0.0 Safari/537.36"
)


@pytest.mark.parametrize(
    ("major", "brand", "header"),
    [
        # Sec-CH-UA values real browsers sent.
        (120, "Google Chrome", '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'),
        (124, "Google Chrome", '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'),
        # Chrome for Testing 156 on this project's CI hosts, measured.
        (156, None, '"Not:A-Brand";v="8", "Chromium";v="156"'),
    ],
)
def test_user_agent_metadata_brands_follow_chromium_grease(
    major: int, brand: str | None, header: str
) -> None:
    metadata = user_agent_metadata(WINDOWS_UA.format(major), brand=brand, full_version="1.2.3.4")

    assert metadata is not None
    sent = ", ".join(f'"{b["brand"]}";v="{b["version"]}"' for b in metadata["brands"])
    assert sent == header
    assert metadata["platform"] == "Windows"
    assert (metadata["architecture"], metadata["bitness"], metadata["mobile"]) == (
        "x86",
        "64",
        False,
    )
    # The running browser's version is used only when its major version matches.
    assert metadata["fullVersion"] == f"{major}.0.0.0"


def test_user_agent_metadata_reads_android_and_macos_details() -> None:
    android = user_agent_metadata(
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/156.0.0.0 Mobile Safari/537.36",
        brand="Google Chrome",
        full_version="156.0.8078.47",
    )
    assert android is not None
    assert (android["platform"], android["platformVersion"], android["model"]) == (
        "Android",
        "13.0.0",
        "Pixel 7",
    )
    assert android["mobile"] is True and android["formFactors"] == ["Mobile"]
    assert android["fullVersion"] == "156.0.8078.47"
    assert {"brand": "Google Chrome", "version": "156.0.8078.47"} in android["fullVersionList"]
    reduced = user_agent_metadata(
        "Mozilla/5.0 (Linux; Android 10; K) Chrome/156.0.0.0 Mobile", brand=None, full_version=""
    )
    assert reduced is not None and reduced["model"] == ""
    mac = user_agent_metadata(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/156.0.0.0 Safari/537.36",
        brand=None,
        full_version="",
    )
    assert mac is not None and (mac["platform"], mac["platformVersion"]) == ("macOS", "10.15.7")


@pytest.mark.parametrize(
    "user_agent",
    ["Custom/1.0", "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"],
)
def test_user_agent_metadata_needs_a_chrome_user_agent(user_agent: str) -> None:
    assert user_agent_metadata(user_agent, brand=None, full_version="156.0.1.2") is None


def test_profile_preserves_caller_accept_header() -> None:
    profile = BrowserProfile(name="custom", extra_http_headers={"Accept": "application/json"})

    assert profile.headers()["Accept"] == "application/json"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("driver", "patchright"),
        ("browser", "firefox"),
        ("slow_mo_ms", 50),
        ("patchright_focus_control", True),
        ("patchright_context_profile", True),
    ],
)
def test_removed_fields_name_their_replacement(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=f"SessionConfig.{field} was removed in WebSkrap 3.0"):
        SessionConfig(**{field: value})


def test_cookie_decline_is_opt_in() -> None:
    assert SessionConfig().decline_cookies is False


def test_native_profile_mode_emulates_nothing() -> None:
    profile = BrowserProfile(
        name="test",
        user_agent="Custom/1.0",
        navigator_languages=["fr-FR", "fr"],
    )
    config = SessionConfig()

    assert config.profile_mode == "native"
    assert config.emulation_commands(profile) == []
    assert config.browser_env(profile) is None


def test_locale_profile_mode_applies_only_what_chrome_owns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    profile = BrowserProfile(
        name="test",
        user_agent="Custom/1.0",
        viewport=Viewport(width=1280, height=720),
        screen=Viewport(width=1440, height=900),
        locale="en-US",
        timezone_id="Europe/Paris",
        color_scheme="dark",
        reduced_motion="reduce",
        extra_http_headers={"X-Test": "1"},
        navigator_languages=["en-US", "en"],
    )

    commands = dict(SessionConfig(profile_mode="locale").emulation_commands(profile))

    # Locale and timezone go in at launch on Linux; see
    # test_locale_profile_sets_timezone_and_languages_at_launch.
    assert set(commands) == {"Emulation.setEmulatedMedia", "Network.setExtraHTTPHeaders"}
    assert commands["Emulation.setEmulatedMedia"]["features"] == [
        {"name": "prefers-color-scheme", "value": "dark"},
        {"name": "prefers-reduced-motion", "value": "reduce"},
    ]
    assert commands["Network.setExtraHTTPHeaders"] == {"headers": {"X-Test": "1"}}


def test_profile_mode_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        SessionConfig(profile_mode="everything")  # type: ignore[arg-type]


def test_headless_chromium_gets_simulated_screen() -> None:
    config = SessionConfig(channel="chrome", headless=True)

    args = config.browser_args()

    assert "--window-size=1840,1000" in args
    assert "--window-position=0,0" in args
    assert "--screen-info={1920x1080 workAreaBottom=80}" in args


def test_fake_media_devices_are_opt_in() -> None:
    flag = "--use-fake-device-for-media-stream"

    assert flag not in SessionConfig().browser_args()
    assert flag in SessionConfig(fake_media_devices=True).browser_args()
    config = SessionConfig(fake_media_devices=True, launch_args=[flag])
    assert config.browser_args().count(flag) == 1


def test_headless_screen_size_is_configurable() -> None:
    config = SessionConfig(headless=True, headless_screen=Viewport(width=1366, height=768))

    args = config.browser_args()

    assert "--window-size=1286,688" in args
    assert "--screen-info={1366x768 workAreaBottom=80}" in args


def test_headless_screen_can_be_disabled() -> None:
    config = SessionConfig(headless=True, headless_screen=None)

    args = config.browser_args()
    assert not any(a.startswith("--window-size") or a.startswith("--screen-info") for a in args)


def test_headed_mode_omits_simulated_screen() -> None:
    config = SessionConfig(channel="chrome", headless=False)

    args = config.browser_args()
    assert not any(a.startswith("--window-size") or a.startswith("--screen-info") for a in args)


def test_user_launch_args_override_simulated_screen() -> None:
    config = SessionConfig(headless=True, launch_args=["--window-size=800,600"])

    args = config.browser_args()

    assert "--window-size=800,600" in args
    assert "--window-size=1840,1000" not in args
    # untouched flags still applied
    assert "--screen-info={1920x1080 workAreaBottom=80}" in args


def test_chromium_disables_automation_controlled() -> None:
    args = SessionConfig().browser_args()
    assert "--disable-blink-features=AutomationControlled" in args


def test_automation_flag_skipped_when_caller_sets_blink_features() -> None:
    config = SessionConfig(launch_args=["--disable-blink-features=AutomationControlled,Foo"])
    args = config.browser_args()
    assert args.count("--disable-blink-features=AutomationControlled") == 0
    assert "--disable-blink-features=AutomationControlled,Foo" in args


def test_mask_headless_user_agent_defaults_off() -> None:
    assert SessionConfig().mask_headless_user_agent is False
    assert SessionConfig(mask_headless_user_agent=True).mask_headless_user_agent is True


def test_reduce_fingerprint_surface_defaults_off() -> None:
    assert SessionConfig().reduce_fingerprint_surface is False
    assert SessionConfig(reduce_fingerprint_surface=True).reduce_fingerprint_surface is True


def test_reduce_fingerprint_surface_adds_chromium_flags() -> None:
    config = SessionConfig(reduce_fingerprint_surface=True)

    args = config.browser_args()

    assert "--disable-webgl" in args
    assert "--disable-reading-from-canvas" in args


def test_reduce_fingerprint_surface_respects_user_launch_args() -> None:
    config = SessionConfig(
        reduce_fingerprint_surface=True,
        launch_args=["--disable-webgl", "--disable-reading-from-canvas"],
    )

    args = config.browser_args()

    assert args.count("--disable-webgl") == 1
    assert args.count("--disable-reading-from-canvas") == 1


def test_webrtc_ip_handling_policy_is_exported() -> None:
    assert "disable_non_proxied_udp" in get_args(WebRtcIPHandlingPolicy)


def test_webrtc_ip_handling_policy_adds_chromium_flags() -> None:
    config = SessionConfig(webrtc_ip_handling_policy="disable_non_proxied_udp")

    args = config.browser_args()

    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert "--force-webrtc-ip-handling-policy" in args


def test_webrtc_ip_handling_policy_respects_user_launch_args() -> None:
    config = SessionConfig(
        webrtc_ip_handling_policy="disable_non_proxied_udp",
        launch_args=[
            "--webrtc-ip-handling-policy=default_public_interface_only",
            "--force-webrtc-ip-handling-policy",
        ],
    )

    args = config.browser_args()

    assert "--webrtc-ip-handling-policy=default_public_interface_only" in args
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" not in args
    assert args.count("--force-webrtc-ip-handling-policy") == 1


@pytest.mark.parametrize(
    "server",
    [
        pytest.param("http://localhost:8080", id="http"),
        pytest.param("https://proxy.test:443", id="https"),
        pytest.param("socks4://proxy.test:1080", id="socks4"),
        pytest.param("socks5://proxy.test:1080", id="socks5"),
    ],
)
def test_proxy_accepts_supported_schemes(server: str) -> None:
    assert ProxyConfig(server=server).server == server


@pytest.mark.parametrize(
    "server",
    [
        pytest.param("localhost:8080", id="no-scheme"),
        pytest.param("ftp://proxy.test", id="unsupported-scheme"),
        pytest.param("HTTP://proxy.test", id="wrong-case"),
        pytest.param("", id="empty"),
        pytest.param("http://user:secret@proxy.test:8080", id="userinfo"),
    ],
)
def test_proxy_requires_supported_scheme(server: str) -> None:
    with pytest.raises(ValidationError):
        ProxyConfig(server=server)


@pytest.mark.parametrize(
    ("width", "height"),
    [
        pytest.param(0, 720, id="zero-width"),
        pytest.param(1280, 0, id="zero-height"),
        pytest.param(-1, 720, id="negative-width"),
        pytest.param(1280, -1, id="negative-height"),
    ],
)
def test_viewport_dimensions_must_be_positive(width: int, height: int) -> None:
    with pytest.raises(ValidationError):
        Viewport(width=width, height=height)


@pytest.mark.parametrize(
    "field",
    [
        pytest.param("name", id="name"),
        pytest.param("locale", id="locale"),
        pytest.param("timezone_id", id="timezone"),
    ],
)
def test_profile_rejects_blank_identity_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        BrowserProfile(**{"name": "test", field: "   "})


def test_profile_without_languages_falls_back_to_its_locale() -> None:
    profile = BrowserProfile(name="test", locale="fr-FR", navigator_languages=[])

    assert profile.navigator_languages == ["fr-FR"]
    assert profile.accept_language() == "fr-FR"


def test_text_window_reports_the_next_offset() -> None:
    window = text_window("abcdef", 2)

    assert window == TextWindow(text="ab", length=6, offset=0, truncated=True, next_offset=2)


def test_text_window_finishes_without_a_next_offset() -> None:
    window = text_window("abcdef", 2, 4)

    assert window.text == "ef"
    assert window.truncated is False
    assert window.next_offset is None


def test_text_window_clamps_instead_of_rejecting() -> None:
    assert text_window("abcdef", 2, 99).text == ""
    assert text_window("abcdef", -5).text == ""
    assert text_window("abcdef", 100).text == "abcdef"


def test_shape_fetch_result_pages_through_a_long_body() -> None:
    result = FetchResult(
        url="https://example.test",
        final_url="https://example.test",
        status=200,
        ok=True,
        headers={},
        text="0123456789",
        title="t",
        cookies=[],
        timings={"elapsed_ms": 1.0},
    )

    first = shape_fetch_result(result, 4)
    assert first["text"] == "0123"
    assert first["text_offset"] == 0
    assert first["text_truncated"] is True
    assert first["next_text_offset"] == 4

    second = shape_fetch_result(result, 4, first["next_text_offset"])
    assert second["text"] == "4567"
    assert second["next_text_offset"] == 8

    last = shape_fetch_result(result, 4, second["next_text_offset"])
    assert last["text"] == "89"
    assert last["text_truncated"] is False
    assert last["next_text_offset"] is None


def _search_result(hits: list[SearchHit], total: int) -> SearchResult:
    return SearchResult(
        query="example domain",
        engine=SearchEngine.DDG,
        url="https://html.duckduckgo.com/html/?q=example+domain",
        final_url="https://html.duckduckgo.com/html/?q=example+domain",
        status=200,
        ok=True,
        hits=hits,
        hits_total=total,
        timings={"elapsed_ms": 12.34},
    )


def test_search_hit_snippet_is_optional() -> None:
    hit = SearchHit(title="Example Domain", url="https://example.com/")

    assert hit.snippet == ""


def test_shape_search_result_reports_what_the_cap_hid() -> None:
    hits = [SearchHit(title="Example Domain", url="https://example.com/", snippet="Illustrative.")]

    payload = shape_search_result(_search_result(hits, 3))

    assert payload == {
        "query": "example domain",
        "engine": "ddg",
        "url": "https://html.duckduckgo.com/html/?q=example+domain",
        "final_url": "https://html.duckduckgo.com/html/?q=example+domain",
        "status": 200,
        "ok": True,
        "hits": [
            {"title": "Example Domain", "url": "https://example.com/", "snippet": "Illustrative."}
        ],
        "hits_total": 3,
        "hits_truncated": True,
        "elapsed_ms": 12.3,
        "cookie_notice_declined": None,
    }


def test_shape_search_result_is_not_truncated_when_every_hit_is_returned() -> None:
    payload = shape_search_result(_search_result([], 0))

    assert payload["hits"] == []
    assert payload["hits_truncated"] is False


def test_search_result_rejects_an_unknown_engine() -> None:
    with pytest.raises(ValidationError):
        SearchResult(
            query="q",
            engine="google",  # type: ignore[arg-type]
            url="https://example.test",
            final_url="https://example.test",
            status=200,
            ok=True,
        )


def test_chromium_stays_sandboxed_by_default() -> None:
    config = SessionConfig()

    assert config.chromium_sandbox is True
    assert "--no-sandbox" not in config.browser_args()


def test_sandbox_opt_out_adds_exactly_one_flag() -> None:
    args = SessionConfig(chromium_sandbox=False).browser_args()

    assert args.count("--no-sandbox") == 1


@pytest.mark.parametrize(
    "flag",
    [
        pytest.param("--no-sandbox", id="no-sandbox"),
        pytest.param("--disable-setuid-sandbox", id="setuid"),
        pytest.param("--load-extension=/tmp/evil", id="load-extension"),
        pytest.param("--unsafely-treat-insecure-origin-as-secure=http://x", id="unsafely"),
        pytest.param("--remote-debugging-port=9222", id="remote-debugging"),
        pytest.param("--remote-allow-origins=*", id="allow-origins"),
    ],
)
def test_dangerous_launch_args_are_blocked(flag: str) -> None:
    with pytest.raises(ValidationError, match="is blocked"):
        SessionConfig(launch_args=[flag])


def test_benign_launch_args_still_pass() -> None:
    config = SessionConfig(launch_args=["--window-size=800,600", "--no-sandboxed-feature"])

    assert "--window-size=800,600" in config.browser_args()


def test_proxy_repr_and_str_redact_credentials() -> None:
    proxy = ProxyConfig(server="http://proxy.test", username="user", password="secret")

    assert "secret" not in repr(proxy)
    assert "'user'" not in repr(proxy)
    assert "secret" not in str(proxy)
    # The browser still gets the real values; only the display is redacted.
    assert proxy.password == "secret"


def test_proxy_redacted_mapping_hides_credentials() -> None:
    redacted = ProxyConfig(
        server="http://proxy.test", username="user", password="secret"
    ).redacted()

    assert redacted["password"] == "***"
    assert redacted["username"] == "***"
    assert redacted["server"] == "http://proxy.test"


def test_shape_fetch_result_filters_response_headers() -> None:
    result = FetchResult(
        url="https://example.test",
        final_url="https://example.test",
        status=200,
        ok=True,
        headers={
            "content-type": "text/html",
            "set-cookie": "session=abc",
            "Set-Cookie": "other=1",
            "authorization": "Bearer x",
            "x-request-id": "42",
        },
        text="body",
        title="t",
        cookies=[],
        timings={"elapsed_ms": 1.0},
    )

    payload = shape_fetch_result(result, 100)

    assert payload["headers"] == {"content-type": "text/html", "x-request-id": "42"}


def test_virtual_display_launches_headed_with_window_but_no_headless_screen() -> None:
    config = SessionConfig(headless=True, virtual_display=True)

    args = config.browser_args()

    assert config.launch_headless is False
    assert "--window-size=1840,1000" in args
    assert "--window-position=0,0" in args
    # --screen-info only exists in headless mode; Xvfb supplies the screen.
    assert not any(a.startswith("--screen-info") for a in args)
    assert config.uses_virtual_display()


def test_virtual_display_screen_follows_headless_screen() -> None:
    sized = SessionConfig(virtual_display=True, headless_screen=Viewport(width=1366, height=768))
    unsized = SessionConfig(virtual_display=True, headless_screen=None)

    assert sized.virtual_screen() == Viewport(width=1366, height=768)
    assert unsized.virtual_screen() == Viewport(width=1920, height=1080)
    assert not any(a.startswith("--window-size") for a in unsized.browser_args())


def test_virtual_display_is_ignored_for_headed_runs() -> None:
    config = SessionConfig(headless=False, virtual_display=True)

    assert config.launch_headless is False
    assert not config.uses_virtual_display()


def _native_config(**overrides: Any) -> SessionConfig:
    return SessionConfig(profile_mode="locale", **overrides)


def _fr_profile() -> BrowserProfile:
    return BrowserProfile(
        name="fr",
        locale="fr-FR",
        timezone_id="Europe/Paris",
        navigator_languages=["fr-FR", "fr", "en-US", "en"],
        color_scheme="dark",
    )


def test_locale_profile_sets_timezone_and_languages_at_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    config = _native_config()
    profile = _fr_profile()

    args = config.browser_args(profile)
    env = config.browser_env(profile)
    commands = dict(config.emulation_commands(profile))

    assert "--accept-lang=fr-FR,fr,en-US,en" in args
    assert env is not None
    assert env["TZ"] == "Europe/Paris"
    # LC_ALL is replaced too: a host value would otherwise pin Intl's locale.
    assert env["LC_ALL"] == env["LANG"] == "fr_FR.UTF-8"
    assert env["LANGUAGE"] == "fr_FR:fr:en_US:en"
    assert "PATH" in env
    # Nothing left to override over CDP; media preferences still apply there.
    assert "Emulation.setLocaleOverride" not in commands
    assert "Emulation.setTimezoneOverride" not in commands
    assert "Emulation.setEmulatedMedia" in commands


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_locale_profile_uses_cdp_overrides_off_linux(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    config = _native_config()
    profile = _fr_profile()

    commands = dict(config.emulation_commands(profile))

    assert config.browser_env(profile) is None
    assert not any(a.startswith("--accept-lang") for a in config.browser_args(profile))
    assert commands["Emulation.setLocaleOverride"] == {"locale": "fr-FR"}
    assert commands["Emulation.setTimezoneOverride"] == {"timezoneId": "Europe/Paris"}
    accept = commands["Network.setExtraHTTPHeaders"]["headers"]["Accept-Language"]
    assert accept.startswith("fr-FR,fr;q=")


def test_native_profile_mode_sets_nothing_at_launch() -> None:
    config = SessionConfig()

    assert config.browser_env(_fr_profile()) is None
    assert not any(a.startswith("--accept-lang") for a in config.browser_args(_fr_profile()))


def test_caller_accept_lang_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    config = _native_config(launch_args=["--accept-lang=de-DE"])

    args = config.browser_args(_fr_profile())

    assert [a for a in args if a.startswith("--accept-lang")] == ["--accept-lang=de-DE"]


def test_unknown_timezone_is_rejected_before_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Chromium would silently report "Etc/Unknown".
    monkeypatch.setattr(sys, "platform", "linux")
    profile = _fr_profile().model_copy(update={"timezone_id": "Not/AZone"})

    with pytest.raises(ValueError, match="unknown timezone 'Not/AZone'"):
        _native_config().browser_env(profile)


def _gl_args(config: SessionConfig) -> list[str]:
    return [
        a
        for a in config.browser_args()
        if a.startswith(("--use-gl", "--use-angle", "--ignore-gpu", "--enable-unsafe-swift"))
    ]


def test_gpu_auto_leaves_headless_webgl_to_chromium() -> None:
    assert SessionConfig().gpu is GpuBackend.AUTO
    assert _gl_args(SessionConfig(headless=True)) == []
    assert _gl_args(SessionConfig(headless=False)) == []


def test_gpu_auto_adds_no_swiftshader_under_a_virtual_display() -> None:
    # Headed Chrome has had no SwiftShader fallback since Chrome 139; a
    # GPU-less desktop shows no WebGL, and forcing SwiftShader reads as a bot.
    assert _gl_args(SessionConfig(virtual_display=True)) == []


@pytest.mark.parametrize("virtual_display", [False, True])
def test_gpu_mesa_renders_through_lavapipe(virtual_display: bool) -> None:
    config = SessionConfig(gpu="mesa", virtual_display=virtual_display)

    assert _gl_args(config) == ["--use-gl=angle", "--use-angle=vulkan", "--ignore-gpu-blocklist"]


def test_caller_gl_flags_win_over_gpu() -> None:
    config = SessionConfig(gpu="mesa", launch_args=["--use-angle=swiftshader"])

    assert _gl_args(config) == ["--use-angle=swiftshader"]


def test_gpu_rejects_unknown_backends() -> None:
    with pytest.raises(ValidationError):
        SessionConfig(gpu="nvidia")  # type: ignore[arg-type]


def _webrtc_args(config: SessionConfig) -> list[str]:
    return [a for a in config.browser_args() if "webrtc" in a]


def test_proxy_disables_non_proxied_udp_by_default() -> None:
    # Unset behind a proxy, ICE would still reveal the host's own addresses.
    config = SessionConfig(proxy=ProxyConfig(server="socks5://proxy.test:1080"))

    assert config.effective_webrtc_ip_handling_policy() == "disable_non_proxied_udp"
    assert _webrtc_args(config) == [
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--force-webrtc-ip-handling-policy",
    ]
    # The field keeps what the caller passed.
    assert config.webrtc_ip_handling_policy is None


def test_explicit_webrtc_policy_wins_behind_a_proxy() -> None:
    config = SessionConfig(
        proxy=ProxyConfig(server="http://proxy.test:8080"), webrtc_ip_handling_policy="default"
    )

    assert _webrtc_args(config)[0] == "--webrtc-ip-handling-policy=default"


def test_no_proxy_leaves_webrtc_to_chromium() -> None:
    assert SessionConfig().effective_webrtc_ip_handling_policy() is None
    assert _webrtc_args(SessionConfig()) == []


def test_headless_declares_a_mouse_unless_the_caller_sets_blink_settings() -> None:
    flag = (
        "--blink-settings=primaryHoverType=2,availableHoverTypes=2,"
        "primaryPointerType=4,availablePointerTypes=4"
    )
    assert flag in SessionConfig(headless=True).browser_args()
    # Headed and virtual-display browsers have a real pointer already.
    assert flag not in SessionConfig(headless=False).browser_args()
    assert flag not in SessionConfig(headless=True, virtual_display=True).browser_args()
    custom = SessionConfig(launch_args=["--blink-settings=imagesEnabled=false"])
    assert [a for a in custom.browser_args() if a.startswith("--blink-settings")] == [
        "--blink-settings=imagesEnabled=false"
    ]
