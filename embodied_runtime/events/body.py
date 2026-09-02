"""Discrete semantic body events."""

from dataclasses import dataclass

from embodied_runtime.events.base import Event


@dataclass(frozen=True, slots=True, kw_only=True)
class BodyOrientationChanged(Event):
    """One completed logical body-orientation transition."""

    previous_yaw_degrees: float
    previous_pitch_degrees: float
    yaw_degrees: float
    pitch_degrees: float
