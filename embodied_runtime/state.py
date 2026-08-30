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
class RuntimeState:
    lifecycle: LifecycleState
    platform: PlatformSnapshot | None = None
