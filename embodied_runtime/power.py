"""Cheap deterministic monitoring of backend-neutral battery voltage."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import logging
import math

from embodied_runtime.events import EventBus, PowerAttentionRequired, PowerRecovered
from embodied_runtime.hardware.base import HardwareBackend
from embodied_runtime.state import PowerCondition, PowerState

LOGGER = logging.getLogger(__name__)
SOURCE = "power_monitor"


@dataclass(frozen=True, slots=True)
class PowerMonitorPolicy:
    """Development defaults for a nominal two-cell supply; tune on hardware."""

    interval_seconds: float = 30.0
    attention_voltage_v: float = 7.4
    recovery_voltage_v: float = 7.7

    def __post_init__(self) -> None:
        values = (self.interval_seconds, self.attention_voltage_v, self.recovery_voltage_v)
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) for value in values):
            raise ValueError("power monitor policy values must be finite numbers")
        if self.interval_seconds <= 0:
            raise ValueError("power monitor interval must be positive")
        if self.attention_voltage_v >= self.recovery_voltage_v:
            raise ValueError("power recovery threshold must exceed attention threshold")


class PowerMonitor:
    def __init__(self, hardware: HardwareBackend, events: EventBus,
                 replace_power: Callable[[PowerState], None],
                 may_publish: Callable[[], bool], clock: Callable[[], datetime], *,
                 policy: PowerMonitorPolicy | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.policy = policy or PowerMonitorPolicy()
        self._hardware, self._events = hardware, events
        self._replace_power, self._may_publish, self._clock = replace_power, may_publish, clock
        self._condition: PowerCondition | None = None
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    @property
    def available(self) -> bool:
        return "battery_voltage" in self._hardware.capabilities

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def sample_once(self) -> PowerState:
        if not self.available:
            state = PowerState(None)
            self._replace_power(state)
            return state
        voltage = self._hardware.read_battery_voltage_v()
        if not math.isfinite(voltage):
            raise ValueError("battery voltage must be finite")
        previous = self._condition
        if previous is PowerCondition.ATTENTION:
            condition = (PowerCondition.NORMAL if voltage >= self.policy.recovery_voltage_v
                         else PowerCondition.ATTENTION)
        else:
            condition = (PowerCondition.ATTENTION if voltage <= self.policy.attention_voltage_v
                         else PowerCondition.NORMAL)
        self._condition = condition
        state = PowerState(voltage, condition, self._clock())
        self._replace_power(state)
        # An initial attention observation is actionable startup reconciliation.
        changed = condition is not previous
        if changed:
            LOGGER.info("[POWER] condition=%s voltage_v=%.3f previous=%s", condition.value,
                        voltage, previous.value if previous else "unknown")
        if self._may_publish() and changed and condition is PowerCondition.ATTENTION:
            await self._events.publish(PowerAttentionRequired(
                source=SOURCE, battery_voltage_v=voltage))
        elif (self._may_publish() and previous is PowerCondition.ATTENTION
              and condition is PowerCondition.NORMAL):
            await self._events.publish(PowerRecovered(source=SOURCE, battery_voltage_v=voltage))
        return state

    def start(self) -> None:
        if self.available and not self.is_running:
            self._task = asyncio.create_task(self._run(), name="power-monitor")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            await self._sleep(self.policy.interval_seconds)
            try:
                await self.sample_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("[POWER] status=sample_failed")
