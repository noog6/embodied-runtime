"""Application lifecycle orchestration."""

import asyncio
from dataclasses import dataclass, replace
import logging

from embodied_runtime.events import (
    ApplicationStarted,
    EventBus,
)
from embodied_runtime.hardware.base import HardwareBackend
from embodied_runtime.profile import RobotProfile
from embodied_runtime.platform import (
    HostPlatformProvider,
    PlatformProvider,
    PlatformSnapshot,
)
from embodied_runtime.state import LifecycleState, RuntimeState

LOGGER = logging.getLogger(__name__)


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
        platform_provider: PlatformProvider | None = None,
    ) -> None:
        self.profile = profile
        self.hardware = hardware
        self.options = options or ApplicationOptions()
        self.events = events or EventBus()
        self._runtime_state = RuntimeState(LifecycleState.CREATED)
        self._platform_provider = platform_provider or HostPlatformProvider()
        self._stop_requested = asyncio.Event()

    @property
    def runtime_state(self) -> RuntimeState:
        return self._runtime_state

    @property
    def state(self) -> LifecycleState:
        """Compatibility view of the authoritative lifecycle state."""
        return self._runtime_state.lifecycle

    def _set_lifecycle(self, lifecycle: LifecycleState) -> None:
        self._runtime_state = replace(self._runtime_state, lifecycle=lifecycle)

    def refresh_platform_state(self) -> PlatformSnapshot:
        snapshot = self._platform_provider.snapshot()
        self._runtime_state = replace(self._runtime_state, platform=snapshot)
        return snapshot

    async def start(self) -> None:
        if self.state is not LifecycleState.CREATED:
            raise RuntimeError(f"Cannot start application in {self.state} state")
        self._set_lifecycle(LifecycleState.STARTING)
        LOGGER.info(
            "[APP] starting profile=%s hardware=%s",
            self.profile.identifier,
            self.hardware.identifier,
        )
        platform_state = self.refresh_platform_state()
        LOGGER.info(
            "[PLATFORM] hostname=%s system=%s machine=%s python=%s status=ready",
            platform_state.hostname,
            platform_state.system,
            platform_state.machine,
            platform_state.python_version,
        )
        await self.events.start()
        try:
            self.hardware.start()
        except BaseException:
            self._set_lifecycle(LifecycleState.STOPPED)
            await self.events.stop()
            raise
        LOGGER.info(
            "[HW] backend=%s physical=%s status=ready",
            self.hardware.identifier,
            str(self.hardware.is_physical).lower(),
        )
        self._set_lifecycle(LifecycleState.RUNNING)
        LOGGER.info("[APP] running profile=%s", self.profile.identifier)
        await self.events.publish(ApplicationStarted(source="application"))

    async def stop(self) -> None:
        if self.state is LifecycleState.STOPPED:
            return
        self._set_lifecycle(LifecycleState.STOPPING)
        LOGGER.info("[APP] stopping")
        try:
            self.hardware.stop()
        finally:
            self._set_lifecycle(LifecycleState.STOPPED)
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
