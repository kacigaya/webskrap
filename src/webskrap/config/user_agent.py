"""Chromium user-agent client hints for browser profiles."""

from __future__ import annotations

import re
from typing import Any

# Chromium's GREASE brand (components/embedder_support/user_agent_utils.cc),
# seeded by the major version, so the brand list matches what that version of
# Chrome sends.
_GREASE_CHARS = (" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_")
_GREASE_VERSIONS = ("8", "99", "24")
_BRAND_ORDERS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))


def _brand_list(major: int, brand: str | None, versions: tuple[str, str]) -> list[dict[str, str]]:
    """Return the brand list Chromium ``major`` sends, in its order.

    ``versions`` holds the GREASE brand's and the real brands' version text.
    """
    grease_name = (
        f"Not{_GREASE_CHARS[major % len(_GREASE_CHARS)]}A"
        f"{_GREASE_CHARS[(major + 1) % len(_GREASE_CHARS)]}Brand"
    )
    grease = {"brand": grease_name, "version": versions[0]}
    chromium = {"brand": "Chromium", "version": versions[1]}
    if brand is None:
        return [grease, chromium] if major % 2 == 0 else [chromium, grease]
    ordered: list[dict[str, str]] = [{}, {}, {}]
    order = _BRAND_ORDERS[major % len(_BRAND_ORDERS)]
    entries = (grease, chromium, {"brand": brand, "version": versions[1]})
    for slot, entry in zip(order, entries, strict=True):
        ordered[slot] = entry
    return ordered


def _three_parts(version: str) -> str:
    parts = [part for part in re.split(r"[._]", version) if part]
    return ".".join((parts + ["0", "0", "0"])[:3])


def _user_agent_platform(user_agent: str) -> str | None:
    """Return a legacy platform when the user agent identifies one.

    Reduced Android user agents omit the CPU. Leave that platform alone
    rather than guessing an architecture from a device name.
    """
    if "Windows NT" in user_agent:
        return "Win32"
    if "Macintosh" in user_agent and "Mac OS X" in user_agent:
        return "MacIntel"
    if linux := re.search(r"\bLinux (x86_64|i[3-6]86|aarch64|armv\d+l)\b", user_agent):
        return f"Linux {linux[1]}"
    if cros := re.search(r"\bCrOS (x86_64|i[3-6]86|aarch64|armv\d+l)\b", user_agent):
        return f"Linux {cros[1]}"
    return None


def user_agent_metadata(
    user_agent: str, *, brand: str | None, full_version: str
) -> dict[str, Any] | None:
    """Return CDP ``userAgentMetadata`` that agrees with ``user_agent``.

    Platform, platform version, model and mobile come from the user-agent
    string; brands follow Chromium's GREASE rules for its ``Chrome/`` major
    version. ``brand`` is the browser's own brand (``Google Chrome``,
    ``Microsoft Edge``, or None for Chromium), so the emulated hints claim
    no other browser than the one running. ``full_version`` is the running
    browser's version; it is used when its major version matches, and
    ``<major>.0.0.0`` otherwise.

    Returns None for a user agent with no ``Chrome/`` token or no known
    platform: the browser cannot claim to be one with no client hints.
    """
    chrome = re.search(r"Chrome/(\d+)\.", user_agent)
    if chrome is None:
        return None
    major = int(chrome[1])
    full = full_version if full_version.split(".")[0] == str(major) else f"{major}.0.0.0"
    mobile = " Mobile" in user_agent
    architecture, bitness, model = "x86", "64", ""
    if android := re.search(r"Android (\d[\d.]*)(?:; ([^;)]+))?", user_agent):
        platform, platform_version = "Android", _three_parts(android[1])
        architecture, bitness = "", ""
        # "K" is the reduced user agent's placeholder for an unknown model.
        model = "" if (android[2] or "K").strip() == "K" else android[2].strip()
    elif "Windows NT" in user_agent:
        platform, platform_version = "Windows", "10.0.0"
    elif mac := re.search(r"Mac OS X (\d+[_.\d]*)", user_agent):
        platform, platform_version = "macOS", _three_parts(mac[1])
    elif "CrOS" in user_agent:
        platform, platform_version = "Chrome OS", ""
    elif "Linux" in user_agent:
        platform, platform_version = "Linux", ""
    else:
        return None
    grease_version = _GREASE_VERSIONS[major % len(_GREASE_VERSIONS)]
    return {
        "brands": _brand_list(major, brand, (grease_version, str(major))),
        "fullVersionList": _brand_list(major, brand, (f"{grease_version}.0.0.0", full)),
        # Deprecated in CDP, but Chromium fills uaFullVersion from it, or from
        # the running browser when it is absent.
        "fullVersion": full,
        "platform": platform,
        "platformVersion": platform_version,
        "architecture": architecture,
        "model": model,
        "mobile": mobile,
        "bitness": bitness,
        "wow64": False,
        "formFactors": ["Mobile"] if mobile else ["Desktop"],
    }
