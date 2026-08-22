"""Bundled browser profiles and lookup by name.

Each profile is a coherent set of viewport, screen, locale, timezone and
language values: mixing a phone viewport with a desktop user agent is exactly
the mismatch fingerprinters look for. Lookups return deep copies, so callers
can edit a profile without changing the bundled one.
"""

from __future__ import annotations

from webskrap.models import BrowserProfile, Viewport

_PROFILES: dict[str, BrowserProfile] = {
    "desktop-chrome": BrowserProfile(
        name="desktop-chrome",
        viewport=Viewport(width=1365, height=768),
        screen=Viewport(width=1440, height=900),
        locale="en-US",
        timezone_id="Europe/Paris",
        device_scale_factor=1,
        navigator_languages=["en-US", "en"],
    ),
    "desktop-edge": BrowserProfile(
        name="desktop-edge",
        viewport=Viewport(width=1440, height=810),
        screen=Viewport(width=1536, height=864),
        locale="en-US",
        timezone_id="Europe/Paris",
        device_scale_factor=1,
        navigator_languages=["en-US", "en"],
    ),
    "mobile-chrome": BrowserProfile(
        name="mobile-chrome",
        user_agent=(
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36"
        ),
        viewport=Viewport(width=412, height=915),
        screen=Viewport(width=412, height=915),
        locale="en-US",
        timezone_id="Europe/Paris",
        device_scale_factor=2.625,
        is_mobile=True,
        has_touch=True,
        navigator_languages=["en-US", "en"],
    ),
}


def list_profiles() -> tuple[BrowserProfile, ...]:
    """Return every bundled profile."""
    return tuple(_PROFILES.values())


def get_profile(name: str | BrowserProfile | None = None) -> BrowserProfile:
    """Return a deep copy of a bundled profile by name.

    Args:
        name: Profile name, an existing profile (returned unchanged), or None
            for ``desktop-chrome``.

    Raises:
        ValueError: If no bundled profile has that name.
    """
    if isinstance(name, BrowserProfile):
        return name
    key = name or "desktop-chrome"
    try:
        return _PROFILES[key].model_copy(deep=True)
    except KeyError as exc:
        available = ", ".join(sorted(_PROFILES))
        msg = f"unknown profile '{key}'. Available profiles: {available}"
        raise ValueError(msg) from exc
