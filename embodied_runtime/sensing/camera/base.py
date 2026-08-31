"""Vendor-neutral contract for one-shot camera acquisition."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class CameraFrame:
    """One immutable encoded image returned as a transient resource."""

    data: bytes
    media_type: str
    width: int
    height: int
    captured_at_ns: int


class CameraBackend(ABC):
    @property
    @abstractmethod
    def identifier(self) -> str: ...

    @property
    @abstractmethod
    def is_physical(self) -> bool: ...

    @property
    @abstractmethod
    def is_running(self) -> bool: ...

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def capture_frame(self) -> CameraFrame: ...
