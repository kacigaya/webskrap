"""Compatibility import for :mod:`webskrap.scraping.client`."""

import sys
from typing import TYPE_CHECKING

from webskrap.scraping import client as _implementation

if TYPE_CHECKING:
    from webskrap.scraping.client import (
        DRM_CLASS_DIR,
        HARDWARE_GPU_VENDORS,
        LINKS_SCRIPT,
        NVIDIA_DEVICE,
        VULKAN_ICD_DIRS,
        T,
        WebSkrapClient,
        WebSkrapError,
        WebSkrapSession,
        browser_doctor,
        effective_gpu,
        hardware_gpu_available,
        lavapipe_available,
        logger,
        require_lavapipe,
        with_channel_fallback,
    )

__all__ = [
    "DRM_CLASS_DIR",
    "HARDWARE_GPU_VENDORS",
    "LINKS_SCRIPT",
    "NVIDIA_DEVICE",
    "T",
    "VULKAN_ICD_DIRS",
    "WebSkrapClient",
    "WebSkrapError",
    "WebSkrapSession",
    "browser_doctor",
    "effective_gpu",
    "hardware_gpu_available",
    "lavapipe_available",
    "logger",
    "require_lavapipe",
    "with_channel_fallback",
]

sys.modules[__name__] = _implementation
