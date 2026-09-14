"""Small immutable snapshots of authoritative runtime state."""

from dataclasses import dataclass
from datetime import datetime
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
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if (self.observed_at is not None and
                (self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None)):
            raise ValueError("power observed_at must be an offset-aware datetime")


@dataclass(frozen=True, slots=True)
class RuntimeState:
    lifecycle: LifecycleState
    platform: PlatformSnapshot | None = None
    body: BodyState | None = None
    presence: PresenceState | None = None
    power: PowerState = PowerState(battery_voltage_v=None)
