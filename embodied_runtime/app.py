"""Application lifecycle orchestration."""

import asyncio
from dataclasses import dataclass
from enum import StrEnum
import logging

from embodied_runtime.events import (
    ApplicationStarted,
    EventBus,
)
from embodied_runtime.hardware.base import HardwareBackend
from embodied_runtime.profile import RobotProfile

LOGGER = logging.getLogger(__name__)


class LifecycleState(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ApplicationOptions:
    startup_prompt: str | None = None


@dataclass(frozen=True)
class RuntimeSummary:
    profile_id: str
    profile_name: str
    hardware_backend: str
    hardware_is_physical: bool
    capabilities: tuple[str, ...]
    startup_prompt_provided: bool
    lifecycle_status: LifecycleState


class RobotApplication:
    def __init__(
        self,
        profile: RobotProfile,
        hardware: HardwareBackend,
        options: ApplicationOptions | None = None,
        events: EventBus | None = None,
    ) -> None:
        self.profile = profile
        self.hardware = hardware
        self.options = options or ApplicationOptions()
        self.events = events or EventBus()
        self.state = LifecycleState.CREATED
        self._stop_requested = asyncio.Event()

    async def start(self) -> None:
        if self.state is not LifecycleState.CREATED:
            raise RuntimeError(f"Cannot start application in {self.state} state")
        self.state = LifecycleState.STARTING
        LOGGER.info(
            "[APP] starting profile=%s hardware=%s",
            self.profile.identifier,
            self.hardware.identifier,
        )
        await self.events.start()
        try:
            self.hardware.start()
        except BaseException:
            self.state = LifecycleState.STOPPED
            await self.events.stop()
            raise
        LOGGER.info(
            "[HW] backend=%s physical=%s status=ready",
            self.hardware.identifier,
            str(self.hardware.is_physical).lower(),
        )
        self.state = LifecycleState.RUNNING
        LOGGER.info("[APP] running profile=%s", self.profile.identifier)
        await self.events.publish(ApplicationStarted(source="application"))

    async def stop(self) -> None:
        if self.state is LifecycleState.STOPPED:
            return
        self.state = LifecycleState.STOPPING
        LOGGER.info("[APP] stopping")
        try:
            self.hardware.stop()
        finally:
            self.state = LifecycleState.STOPPED
            self._stop_requested.set()
            await self.events.stop()
            LOGGER.info("[APP] stopped")

    async def run(self) -> None:
        await self.start()
        try:
            await self._stop_requested.wait()
        except asyncio.CancelledError:
            LOGGER.info("[APP] interrupted")
            raise
        except KeyboardInterrupt:
            LOGGER.info("[APP] interrupted")
        finally:
            await self.stop()

    def request_stop(self) -> None:
        """Request an orderly stop from code running on the application loop."""
        self._stop_requested.set()

    def summary(self) -> RuntimeSummary:
        return RuntimeSummary(
            profile_id=self.profile.identifier,
            profile_name=self.profile.name,
            hardware_backend=self.hardware.identifier,
            hardware_is_physical=self.hardware.is_physical,
            capabilities=tuple(self.hardware.capabilities),
            startup_prompt_provided=self.options.startup_prompt is not None,
            lifecycle_status=self.state,
        )
