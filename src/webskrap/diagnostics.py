"""One call that answers "why is this not working here?".

:func:`diagnose` collects everything the CLI's ``doctor`` and the MCP tool of
the same name report: whether a browser launches, which versions are installed,
where WebSkrap reads and writes, which environment overrides are in force, and
which browser sessions exist. A caller debugging a failure otherwise has to ask
five separate questions and already know which five.

Only paths and version strings are reported. Nothing here reads cookies,
storage state, or proxy credentials.
"""

from __future__ import annotations

import os
import platform
from importlib.metadata import PackageNotFoundError, version
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
        "sessions": browser_session.list_sessions(),
    }
