"""Immediate, hardware-free semantic body implementation."""

import math

from embodied_runtime.body.base import BodyBackend
from embodied_runtime.state import BodyState


class VirtualBodyBackend(BodyBackend):
    identifier = "virtual"
    is_physical = False
    capabilities = ("orientation",)

    def __init__(self) -> None:
        self._running = False

    async def start(self) -> BodyState:
        self._running = True
        return BodyState(yaw_degrees=0.0, pitch_degrees=0.0)

    async def stop(self) -> None:
        self._running = False

    async def set_orientation(
        self, yaw_degrees: float, pitch_degrees: float
    ) -> BodyState:
        if not self._running:
            raise RuntimeError("Body backend is not running")
        yaw = self._validate("yaw", yaw_degrees, -180.0, 180.0)
        pitch = self._validate("pitch", pitch_degrees, -90.0, 90.0)
        return BodyState(yaw_degrees=yaw, pitch_degrees=pitch)

    @staticmethod
    def _validate(name: str, value: float, lower: float, upper: float) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must be a finite number") from error
        if not math.isfinite(result):
            raise ValueError(f"{name} must be a finite number")
        if not lower <= result <= upper:
            raise ValueError(
                f"{name} must be between {lower:g} and {upper:g} degrees"
            )
        return result
