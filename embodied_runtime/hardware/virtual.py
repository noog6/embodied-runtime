"""Hardware-free backend for development and future virtual embodiments."""

from embodied_runtime.hardware.base import HardwareBackend


class VirtualHardwareBackend(HardwareBackend):
    def __init__(self) -> None:
        self._running = False

    @property
    def identifier(self) -> str:
        return "virtual"

    @property
    def is_physical(self) -> bool:
        return False

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
