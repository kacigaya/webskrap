"""Compatibility import for :mod:`webskrap.interfaces.cli`."""

import sys
from typing import TYPE_CHECKING

from webskrap.interfaces import cli as _implementation

if TYPE_CHECKING:
    from webskrap.interfaces.cli import (
        ChannelOption,
        DeclineCookiesTimeoutMsOption,
        FakeMediaDevicesOption,
        LaunchArgsOption,
        MaskHeadlessUserAgentOption,
        NoSandboxOption,
        OutputFormatOption,
        ProfileModeOption,
        ReduceFingerprintSurfaceOption,
        T,
        UserDataDirOption,
        WebrtcIpHandlingPolicyOption,
        app,
        console,
        describe_command,
        doctor_command,
        fetch_command,
        install_command,
        mcp_command,
        profiles_command,
        schema_command,
        search_command,
    )

__all__ = [
    "ChannelOption",
    "DeclineCookiesTimeoutMsOption",
    "FakeMediaDevicesOption",
    "LaunchArgsOption",
    "MaskHeadlessUserAgentOption",
    "NoSandboxOption",
    "OutputFormatOption",
    "ProfileModeOption",
    "ReduceFingerprintSurfaceOption",
    "T",
    "UserDataDirOption",
    "WebrtcIpHandlingPolicyOption",
    "app",
    "console",
    "describe_command",
    "doctor_command",
    "fetch_command",
    "install_command",
    "mcp_command",
    "profiles_command",
    "schema_command",
    "search_command",
]

sys.modules[__name__] = _implementation
