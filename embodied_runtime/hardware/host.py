"""Physical host backend without specialized robot I/O."""

from embodied_runtime.hardware.base import HardwareBackend


class HostHardwareBackend(HardwareBackend):
    """Represent the physical computer when no robot I/O board is available."""

    identifier = "host"
    is_physical = True

    def __init__(self) -> None:
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def capabilities(self) -> tuple[str, ...]:
        return ()

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False
