from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from webskrap import (
    BrowserProfile,
    ProxyConfig,
    ResourcePolicy,
    SessionConfig,
    Viewport,
    WebRtcIPHandlingPolicy,
)


def test_profile_generates_context_options() -> None:
    profile = BrowserProfile(
        name="test",
        viewport=Viewport(width=1280, height=720),
        locale="en-US",
        timezone_id="Europe/Paris",
        navigator_languages=["en-US", "en"],
    )

    options = profile.to_context_options()

    assert options["viewport"] == {"width": 1280, "height": 720}
    assert options["locale"] == "en-US"
    assert options["timezone_id"] == "Europe/Paris"
    assert options["extra_http_headers"]["Accept-Language"] == "en-US,en;q=0.9"


def test_session_config_maps_proxy_and_storage_state() -> None:
    profile = BrowserProfile(name="test")
    config = SessionConfig(
        proxy=ProxyConfig(server="http://localhost:8080", username="user", password="pass"),
        storage_state={"cookies": [], "origins": []},
        resource_policy=ResourcePolicy.LITE,
    )

    options = config.context_options(profile)

    assert options["proxy"] == {
        "server": "http://localhost:8080",
        "username": "user",
        "password": "pass",
    }
    assert options["storage_state"] == {"cookies": [], "origins": []}


def test_cookie_decline_is_opt_in() -> None:
    assert SessionConfig().decline_cookies is False


def test_patchright_context_omits_profile_by_default() -> None:
    profile = BrowserProfile(
        name="test",
        user_agent="Custom/1.0",
        navigator_languages=["fr-FR", "fr"],
    )
    config = SessionConfig(driver="patchright", headless=True)

    options = config.context_options(profile)

    assert options["no_viewport"] is True
    assert "focus_control" not in options
    assert "user_agent" not in options
    assert "extra_http_headers" not in options


def test_patchright_context_profile_applies_native_context_metadata() -> None:
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
    config = SessionConfig(driver="patchright", patchright_context_profile=True)

    options = config.context_options(profile)

    assert options["no_viewport"] is True
    assert "focus_control" not in options
    assert options["locale"] == "en-US"
    assert options["timezone_id"] == "Europe/Paris"
    assert options["color_scheme"] == "dark"
    assert options["reduced_motion"] == "reduce"
    assert options["extra_http_headers"] == {"X-Test": "1"}
    assert "viewport" not in options
    assert "screen" not in options
    assert "user_agent" not in options
    assert "device_scale_factor" not in options
    assert "is_mobile" not in options
    assert "has_touch" not in options


def test_patchright_focus_control_can_be_omitted() -> None:
    profile = BrowserProfile(name="test")
    config = SessionConfig(driver="patchright", patchright_focus_control=None)

    options = config.context_options(profile)

    assert options["no_viewport"] is True
    assert "focus_control" not in options


def test_patchright_focus_control_omitted_by_default() -> None:
    profile = BrowserProfile(name="test")
    config = SessionConfig(driver="patchright")

    options = config.context_options(profile)

    assert "focus_control" not in options


def test_patchright_focus_control_emitted_when_set() -> None:
    profile = BrowserProfile(name="test")
    config = SessionConfig(driver="patchright", patchright_focus_control=True)

    options = config.context_options(profile)

    assert options["focus_control"] is True


def test_headless_chromium_gets_simulated_screen() -> None:
    config = SessionConfig(driver="patchright", channel="chrome", headless=True)

    args = config.launch_options()["args"]

    assert "--window-size=1920,1080" in args
    assert "--window-position=0,0" in args
    assert "--screen-info={1920x1080}" in args


def test_headless_screen_size_is_configurable() -> None:
    config = SessionConfig(headless=True, headless_screen=Viewport(width=1366, height=768))

    args = config.launch_options()["args"]

    assert "--window-size=1366,768" in args
    assert "--screen-info={1366x768}" in args


def test_headless_screen_can_be_disabled() -> None:
    config = SessionConfig(headless=True, headless_screen=None)

    args = config.launch_options()["args"]
    assert not any(a.startswith("--window-size") or a.startswith("--screen-info") for a in args)


def test_headed_mode_omits_simulated_screen() -> None:
    config = SessionConfig(driver="patchright", channel="chrome", headless=False)

    args = config.launch_options().get("args", [])
    assert not any(a.startswith("--window-size") or a.startswith("--screen-info") for a in args)


def test_user_launch_args_override_simulated_screen() -> None:
    config = SessionConfig(headless=True, launch_args=["--window-size=800,600"])

    args = config.launch_options()["args"]

    assert "--window-size=800,600" in args
    assert "--window-size=1920,1080" not in args
    # untouched flags still applied
    assert "--screen-info={1920x1080}" in args


def test_non_chromium_headless_omits_simulated_screen() -> None:
    config = SessionConfig(browser="firefox", headless=True)

    assert "args" not in config.launch_options()


def test_chromium_disables_automation_controlled() -> None:
    args = SessionConfig(browser="chromium").launch_options()["args"]
    assert "--disable-blink-features=AutomationControlled" in args


def test_patchright_omits_automation_controlled_flag() -> None:
    args = SessionConfig(driver="patchright", browser="chromium").launch_options()["args"]
    assert "--disable-blink-features=AutomationControlled" not in args


def test_automation_flag_skipped_when_caller_sets_blink_features() -> None:
    config = SessionConfig(launch_args=["--disable-blink-features=AutomationControlled,Foo"])
    args = config.launch_options()["args"]
    assert args.count("--disable-blink-features=AutomationControlled") == 0
    assert "--disable-blink-features=AutomationControlled,Foo" in args


def test_non_chromium_omits_automation_flag() -> None:
    assert "args" not in SessionConfig(browser="firefox", headless=False).launch_options()


def test_mask_headless_user_agent_defaults_off() -> None:
    assert SessionConfig().mask_headless_user_agent is False
    assert SessionConfig(mask_headless_user_agent=True).mask_headless_user_agent is True


def test_reduce_fingerprint_surface_defaults_off() -> None:
    assert SessionConfig().reduce_fingerprint_surface is False
    assert SessionConfig(reduce_fingerprint_surface=True).reduce_fingerprint_surface is True


def test_reduce_fingerprint_surface_adds_chromium_flags() -> None:
    config = SessionConfig(reduce_fingerprint_surface=True)

    args = config.launch_options()["args"]

    assert "--disable-webgl" in args
    assert "--disable-reading-from-canvas" in args


def test_reduce_fingerprint_surface_respects_user_launch_args() -> None:
    config = SessionConfig(
        reduce_fingerprint_surface=True,
        launch_args=["--disable-webgl", "--disable-reading-from-canvas"],
    )

    args = config.launch_options()["args"]

    assert args.count("--disable-webgl") == 1
    assert args.count("--disable-reading-from-canvas") == 1


def test_reduce_fingerprint_surface_is_chromium_only() -> None:
    config = SessionConfig(
        browser="firefox",
        headless=False,
        reduce_fingerprint_surface=True,
    )

    assert "args" not in config.launch_options()


def test_webrtc_ip_handling_policy_is_exported() -> None:
    assert "disable_non_proxied_udp" in get_args(WebRtcIPHandlingPolicy)


def test_webrtc_ip_handling_policy_adds_chromium_flags() -> None:
    config = SessionConfig(webrtc_ip_handling_policy="disable_non_proxied_udp")

    args = config.launch_options()["args"]

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

    args = config.launch_options()["args"]

    assert "--webrtc-ip-handling-policy=default_public_interface_only" in args
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" not in args
    assert args.count("--force-webrtc-ip-handling-policy") == 1


def test_webrtc_ip_handling_policy_is_chromium_only() -> None:
    config = SessionConfig(
        browser="firefox",
        headless=False,
        webrtc_ip_handling_policy="disable_non_proxied_udp",
    )

    assert "args" not in config.launch_options()


def test_proxy_requires_supported_scheme() -> None:
    with pytest.raises(ValidationError):
        ProxyConfig(server="localhost:8080")


def test_viewport_dimensions_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Viewport(width=0, height=720)
