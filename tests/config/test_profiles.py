from __future__ import annotations

import pytest

from webskrap.profiles import get_profile, list_profiles


def test_builtin_profiles_are_available() -> None:
    names = {profile.name for profile in list_profiles()}

    assert {"desktop-chrome", "desktop-edge", "mobile-chrome"} <= names


def test_get_profile_returns_copy() -> None:
    first = get_profile("desktop-chrome")
    second = get_profile("desktop-chrome")

    first.locale = "fr-FR"

    assert second.locale == "en-US"


def test_unknown_profile_lists_available_names() -> None:
    with pytest.raises(ValueError, match="Available profiles"):
        get_profile("missing")
