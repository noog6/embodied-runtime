"""Semantic transitions derived from host platform observations."""

from dataclasses import dataclass
from embodied_runtime.events.base import Event


@dataclass(frozen=True, slots=True, kw_only=True)
class ThermalWarningRaised(Event):
    cpu_temperature_celsius: float
    warning_threshold_celsius: float


@dataclass(frozen=True, slots=True, kw_only=True)
class ThermalWarningCleared(Event):
    cpu_temperature_celsius: float
    clear_threshold_celsius: float


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryPressureRaised(Event):
    memory_available_bytes: int
    memory_total_bytes: int
    available_ratio: float
    pressure_threshold_ratio: float


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryPressureCleared(Event):
    memory_available_bytes: int
    memory_total_bytes: int
    available_ratio: float
    clear_threshold_ratio: float
