"""Async platform sampling and advisory condition transitions."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
import math
from embodied_runtime.events import EventBus
from embodied_runtime.events.platform import (
    MemoryPressureCleared,
    MemoryPressureRaised,
    ThermalWarningCleared,
    ThermalWarningRaised,
)
from embodied_runtime.platform.base import PlatformProvider, PlatformSnapshot

LOGGER = logging.getLogger(__name__)
SOURCE = "platform_monitor"


@dataclass(frozen=True, slots=True)
class PlatformMonitorPolicy:
    interval_seconds: float = 5.0
    thermal_warning_celsius: float = 80.0
    thermal_clear_celsius: float = 75.0
    memory_pressure_ratio: float = 0.10
    memory_clear_ratio: float = 0.15

    def __post_init__(self) -> None:
        values = (
            self.interval_seconds,
            self.thermal_warning_celsius,
            self.thermal_clear_celsius,
            self.memory_pressure_ratio,
            self.memory_clear_ratio,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("platform monitor policy values must be finite")
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self.thermal_clear_celsius >= self.thermal_warning_celsius:
            raise ValueError("thermal clear threshold must be below warning threshold")
        if not 0 <= self.memory_pressure_ratio < self.memory_clear_ratio <= 1:
            raise ValueError("memory ratios must satisfy 0 <= pressure < clear <= 1")


class PlatformMonitor:
    """One app-owned polling task; not a telemetry service or state owner."""

    def __init__(
        self,
        provider: PlatformProvider,
        events: EventBus,
        replace_platform: Callable[[PlatformSnapshot], None],
        may_publish: Callable[[], bool],
        *,
        policy: PlatformMonitorPolicy | None = None,
    ) -> None:
        self.policy = policy or PlatformMonitorPolicy()
        self._provider = provider
        self._events = events
        self._replace_platform = replace_platform
        self._may_publish = may_publish
        self._thermal_warning: bool | None = None
        self._memory_pressure: bool | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def establish_baseline(self, snapshot: PlatformSnapshot) -> None:
        value = snapshot.cpu_temperature_celsius
        if value is not None and math.isfinite(value):
            self._thermal_warning = value >= self.policy.thermal_warning_celsius
        ratio = self._memory_ratio(snapshot)
        if ratio is not None:
            self._memory_pressure = ratio <= self.policy.memory_pressure_ratio

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError("platform monitor is already running")
        self._task = asyncio.create_task(self._run(), name="platform-monitor")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def sample_platform_once(self) -> PlatformSnapshot:
        snapshot = self._provider.snapshot()
        self._replace_platform(snapshot)
        if self._may_publish():
            await self._evaluate_thermal(snapshot)
            if self._may_publish():
                await self._evaluate_memory(snapshot)
        return snapshot

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.policy.interval_seconds)
            try:
                await self.sample_platform_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.exception(
                    "[PULSE] monitor=platform status=sample_failed error=%s", error
                )

    async def _evaluate_thermal(self, snapshot: PlatformSnapshot) -> None:
        value = snapshot.cpu_temperature_celsius
        if value is None or not math.isfinite(value):
            return
        if (
            self._thermal_warning is not True
            and value >= self.policy.thermal_warning_celsius
        ):
            self._thermal_warning = True
            LOGGER.warning("[PULSE] thermal_warning=raised cpu_temp_c=%.1f", value)
            await self._events.publish(
                ThermalWarningRaised(
                    source=SOURCE,
                    cpu_temperature_celsius=value,
                    warning_threshold_celsius=self.policy.thermal_warning_celsius,
                )
            )
        elif (
            self._thermal_warning is True and value <= self.policy.thermal_clear_celsius
        ):
            self._thermal_warning = False
            LOGGER.info("[PULSE] thermal_warning=cleared cpu_temp_c=%.1f", value)
            await self._events.publish(
                ThermalWarningCleared(
                    source=SOURCE,
                    cpu_temperature_celsius=value,
                    clear_threshold_celsius=self.policy.thermal_clear_celsius,
                )
            )
        elif self._thermal_warning is None:
            self._thermal_warning = False

    async def _evaluate_memory(self, snapshot: PlatformSnapshot) -> None:
        ratio = self._memory_ratio(snapshot)
        if ratio is None:
            return
        available, total = snapshot.memory_available_bytes, snapshot.memory_total_bytes
        assert available is not None and total is not None
        if (
            self._memory_pressure is not True
            and ratio <= self.policy.memory_pressure_ratio
        ):
            self._memory_pressure = True
            LOGGER.warning("[PULSE] memory_pressure=raised available_ratio=%.3f", ratio)
            await self._events.publish(
                MemoryPressureRaised(
                    source=SOURCE,
                    memory_available_bytes=available,
                    memory_total_bytes=total,
                    available_ratio=ratio,
                    pressure_threshold_ratio=self.policy.memory_pressure_ratio,
                )
            )
        elif self._memory_pressure is True and ratio >= self.policy.memory_clear_ratio:
            self._memory_pressure = False
            LOGGER.info("[PULSE] memory_pressure=cleared available_ratio=%.3f", ratio)
            await self._events.publish(
                MemoryPressureCleared(
                    source=SOURCE,
                    memory_available_bytes=available,
                    memory_total_bytes=total,
                    available_ratio=ratio,
                    clear_threshold_ratio=self.policy.memory_clear_ratio,
                )
            )
        elif self._memory_pressure is None:
            self._memory_pressure = False

    @staticmethod
    def _memory_ratio(snapshot: PlatformSnapshot) -> float | None:
        total, available = snapshot.memory_total_bytes, snapshot.memory_available_bytes
        if (
            total is None
            or available is None
            or total <= 0
            or available < 0
            or available > total
        ):
            return None
        return available / total
