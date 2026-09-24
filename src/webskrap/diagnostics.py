"""One call that answers "why is this not working here?".

:func:`diagnose` collects everything the CLI's ``doctor`` and the MCP tool of
the same name report: whether a browser launches, which versions are installed,
where WebSkrap reads and writes, which environment overrides are in force, and
which browser sessions exist, plus host facts pages can read, such as a
UTC timezone. A caller debugging a failure otherwise has to ask
five separate questions and already know which five.

Only paths and version strings are reported. Nothing here reads cookies,
storage state, or proxy credentials.
"""

from __future__ import annotations

import os
import platform
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from webskrap import browser_session
from webskrap.client import browser_doctor
from webskrap.paths import MCP_PROFILE_DIR_ENV, OUTPUT_DIR_ENV, mcp_profile_root, output_root

#: Environment variables that change where WebSkrap reads and writes, or how it
#: launches. All hold paths or flags; none holds a credential.
ENVIRONMENT_VARIABLES = (
    "WEBSKRAP_BROWSER_DIR",
    browser_session.SANDBOX_ENV,
    MCP_PROFILE_DIR_ENV,
    OUTPUT_DIR_ENV,
)


#: Zone names that all mean UTC. A browser reporting one is almost always a
#: server: consumer machines keep a local zone.
UTC_ZONES = frozenset(
    {"UTC", "Etc/UTC", "Etc/Universal", "Universal", "Zulu", "Etc/Zulu", "GMT", "Etc/GMT"}
)


def host_timezone() -> str:
    """Return the timezone a browser launched from here inherits.

    ``TZ`` wins, then the ``/etc/localtime`` symlink target, then the C
    library's abbreviation, which is less precise but never empty.
    """
    if zone := os.environ.get("TZ", "").lstrip(":"):
        return zone
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = str(localtime.resolve())
        if "/zoneinfo/" in target:
            return target.split("/zoneinfo/", 1)[1]
    return time.tzname[0]


def package_version(name: str) -> str | None:
    """Return an installed distribution's version, or None when it is absent."""
    try:
        return version(name)
    except PackageNotFoundError:  # pragma: no cover - depends on the install
        return None


async def diagnose() -> dict[str, Any]:
    """Return the full readiness report.

    Launches one headless Chromium through :func:`~webskrap.client.browser_doctor`
    and adds the surrounding facts. ``ok`` reflects the launch alone, so a
    report can be ``ok`` while listing sessions that are not running.
    """
    probe = await browser_doctor()
    timezone = host_timezone()
    warnings: list[str] = []
    if timezone in UTC_ZONES:
        warnings.append(
            f"Host timezone is {timezone}, which pages read as a server. Apply a profile "
            "timezone that matches your exit IP (patchright_context_profile / "
            "--patchright-context-profile) or set TZ."
        )
    return {
        **probe,
        "versions": {
            "webskrap": package_version("webskrap"),
            "playwright": package_version("playwright"),
            "patchright": package_version("patchright"),
            "mcp": package_version("mcp"),
            "python": platform.python_version(),
        },
        "platform": f"{platform.system()} {platform.machine()}",
        "paths": {
            "sessions_root": str(browser_session.sessions_root()),
            "output_root": str(output_root()),
            "mcp_profile_root": str(mcp_profile_root()),
        },
        # Reported as None when unset, so a caller can tell "left at the
        # default" from "explicitly set to the default".
        "environment": {name: os.environ.get(name) for name in ENVIRONMENT_VARIABLES},
        "chromium_sandbox": browser_session.sandbox_enabled(),
        # One-shot fetch/search resolve the same default through their own
        # config: sandboxed unless the operator opted out above.
        "chromium_sandbox_one_shot": browser_session.sandbox_enabled(None),
        "sessions_root_symlink": browser_session.sessions_root().is_symlink(),
        "sessions": browser_session.list_sessions(),
        "host_timezone": timezone,
        "warnings": warnings,
    }
