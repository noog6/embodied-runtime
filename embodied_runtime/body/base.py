"""Focused semantic embodiment contract."""

from abc import ABC, abstractmethod

from embodied_runtime.state import BodyState


class BodyBackend(ABC):
    @property
    @abstractmethod
    def identifier(self) -> str: ...

    @property
    @abstractmethod
    def is_physical(self) -> bool: ...

    @property
    @abstractmethod
    def capabilities(self) -> tuple[str, ...]: ...

    @abstractmethod
    async def start(self) -> BodyState: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def set_orientation(
        self, yaw_degrees: float, pitch_degrees: float
    ) -> BodyState: ...
