"""Platform observations exposed to the runtime."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PlatformSnapshot:
    """An immutable observation of the computer hosting the runtime."""

    hostname: str
    system: str
    release: str
    machine: str
    python_version: str
    model: str | None
    uptime_seconds: float | None
    load_averages: tuple[float, float, float] | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    cpu_temperature_celsius: float | None
    captured_monotonic: float


class PlatformProvider(Protocol):
    """Source of local platform observations."""

    def snapshot(self) -> PlatformSnapshot: ...
