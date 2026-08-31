"""Typed, transient events for in-process runtime communication."""

from embodied_runtime.events.base import Event
from embodied_runtime.events.bus import EventBus, Subscription
from embodied_runtime.events.lifecycle import ApplicationStarted
from embodied_runtime.events.presence import PresenceChanged
from embodied_runtime.events.platform import (
    MemoryPressureCleared,
    MemoryPressureRaised,
    ThermalWarningCleared,
    ThermalWarningRaised,
)

__all__ = [
    "ApplicationStarted",
    "Event",
    "EventBus",
    "MemoryPressureCleared",
    "MemoryPressureRaised",
    "PresenceChanged",
    "Subscription",
    "ThermalWarningCleared",
    "ThermalWarningRaised",
]
