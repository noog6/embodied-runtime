"""Hardware backend contracts and implementations."""

from embodied_runtime.hardware.fusion_hat import (
    FusionHatHardwareBackend,
    FusionHatPwmChannel,
    FusionHatSysfs,
    FusionHatUnavailableError,
)
from embodied_runtime.hardware.base import HardwareBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend

__all__ = [
    "FusionHatHardwareBackend", "FusionHatPwmChannel", "FusionHatSysfs",
    "FusionHatUnavailableError",
    "HardwareBackend", "VirtualHardwareBackend",
]
