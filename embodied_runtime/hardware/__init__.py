"""Hardware backend contracts and implementations."""

from embodied_runtime.hardware.base import HardwareBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend

__all__ = ["HardwareBackend", "VirtualHardwareBackend"]
