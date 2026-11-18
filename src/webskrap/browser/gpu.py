"""GPU backend selection for Chromium launches."""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from pathlib import Path

from webskrap.config.models import GpuBackend
from webskrap.errors import (
    ErrorCode,
    WebSkrapError,
)

#: Where the Vulkan loader looks for driver manifests on Linux.
VULKAN_ICD_DIRS = (
    Path("/usr/share/vulkan/icd.d"),
    Path("/usr/local/share/vulkan/icd.d"),
    Path("/etc/vulkan/icd.d"),
)


def lavapipe_available() -> bool:
    """True when Mesa's software Vulkan driver (lavapipe) is installed."""
    if not sys.platform.startswith("linux"):
        return False
    if os.environ.get("VK_DRIVER_FILES") or os.environ.get("VK_ICD_FILENAMES"):
        # An explicit driver list is the caller's choice; trust it.
        return True
    return any(
        any(directory.glob("lvp_icd*.json")) for directory in VULKAN_ICD_DIRS if directory.is_dir()
    )


DRM_CLASS_DIR = Path("/sys/class/drm")
NVIDIA_DEVICE = Path("/dev/nvidia0")
HARDWARE_GPU_VENDORS = frozenset({"0x8086", "0x1002", "0x10de"})


def hardware_gpu_available() -> bool:
    """True when an Intel, AMD or NVIDIA GPU is present on this Linux host."""
    if NVIDIA_DEVICE.exists():
        return True
    for node in DRM_CLASS_DIR.glob("renderD*"):
        with suppress(OSError):
            if (node / "device" / "vendor").read_text().strip() in HARDWARE_GPU_VENDORS:
                return True
    return False


def effective_gpu(gpu: GpuBackend, *, virtual_display: bool) -> GpuBackend:
    """Return the GL backend a launch uses for the requested ``gpu``.

    Headed Chrome on a GPU-less host has no WebGL, so ``auto`` under a
    virtual display becomes ``mesa`` when lavapipe is installed and no
    hardware GPU is found.
    """
    if (
        gpu is GpuBackend.AUTO
        and virtual_display
        and lavapipe_available()
        and not hardware_gpu_available()
    ):
        return GpuBackend.MESA
    return gpu


def require_lavapipe() -> None:
    """Raise ``browser_launch`` unless Mesa's lavapipe driver is installed.

    Without the driver Chromium does not fall back: WebGL just disappears,
    which is not what a caller asking for Mesa wanted.
    """
    if not lavapipe_available():
        msg = (
            "gpu='mesa' needs Linux with Mesa's lavapipe Vulkan driver. "
            "Install it (Debian/Ubuntu: apt install mesa-vulkan-drivers)"
        )
        raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH)
