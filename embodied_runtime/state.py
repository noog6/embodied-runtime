"""Small immutable snapshots of authoritative runtime state."""

from dataclasses import dataclass
from enum import StrEnum

from embodied_runtime.platform import PlatformSnapshot


class LifecycleState(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class BodyState:
    yaw_degrees: float
    pitch_degrees: float


@dataclass(frozen=True, slots=True)
class PresenceState:
    present: bool
    source: str


@dataclass(frozen=True, slots=True)
class PowerState:
    battery_voltage_v: float | None


@dataclass(frozen=True, slots=True)
class RuntimeState:
    lifecycle: LifecycleState
    platform: PlatformSnapshot | None = None
    body: BodyState | None = None
    presence: PresenceState | None = None
    power: PowerState = PowerState(battery_voltage_v=None)
