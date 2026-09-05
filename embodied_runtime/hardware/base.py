"""Contract shared by hardware backends."""

from abc import ABC, abstractmethod
from collections.abc import Sequence


class HardwareBackend(ABC):
    @property
    @abstractmethod
    def identifier(self) -> str: ...

    @property
    @abstractmethod
    def is_physical(self) -> bool: ...

    @property
    @abstractmethod
    def is_running(self) -> bool: ...

    @property
    @abstractmethod
    def capabilities(self) -> Sequence[str]: ...

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    def read_battery_voltage_v(self) -> float:
        """Read current battery voltage when the advertised capability exists."""
        raise RuntimeError("Battery-voltage capability is unavailable")
