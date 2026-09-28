"""Semantic power-condition transition events."""

from dataclasses import dataclass

from embodied_runtime.events.base import Event


@dataclass(frozen=True, slots=True, kw_only=True)
class PowerAttentionRequired(Event):
    battery_voltage_v: float


@dataclass(frozen=True, slots=True, kw_only=True)
class PowerRecovered(Event):
    battery_voltage_v: float
