"""Small provider-neutral projections of semantic runtime transitions."""

from dataclasses import dataclass

from embodied_runtime.events import (
    BodyOrientationChanged,
    MemoryPressureCleared,
    MemoryPressureRaised,
    ThermalWarningCleared,
    ThermalWarningRaised,
)


@dataclass(frozen=True, slots=True)
class SemanticObservationFact:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class SemanticObservation:
    """A request-local description of one already-known semantic transition."""

    kind: str
    source: str
    facts: tuple[SemanticObservationFact, ...]


def observation_from_body_orientation(
    event: BodyOrientationChanged,
) -> SemanticObservation:
    return SemanticObservation(
        "body_orientation_changed",
        event.source,
        (
            SemanticObservationFact("previous_yaw_deg", str(event.previous_yaw_degrees)),
            SemanticObservationFact("previous_pitch_deg", str(event.previous_pitch_degrees)),
            SemanticObservationFact("yaw_deg", str(event.yaw_degrees)),
            SemanticObservationFact("pitch_deg", str(event.pitch_degrees)),
        ),
    )


_PLATFORM_TRANSITIONS = {
    ThermalWarningRaised: ("thermal_warning_raised", "thermal", "raised"),
    ThermalWarningCleared: ("thermal_warning_cleared", "thermal", "cleared"),
    MemoryPressureRaised: ("memory_pressure_raised", "memory_pressure", "raised"),
    MemoryPressureCleared: ("memory_pressure_cleared", "memory_pressure", "cleared"),
}


def observation_from_platform_transition(
    event: ThermalWarningRaised | ThermalWarningCleared |
    MemoryPressureRaised | MemoryPressureCleared,
) -> SemanticObservation:
    try:
        kind, condition, transition = _PLATFORM_TRANSITIONS[type(event)]
    except KeyError as error:
        raise TypeError("unsupported platform transition") from error
    return SemanticObservation(
        kind,
        event.source,
        (
            SemanticObservationFact("condition", condition),
            SemanticObservationFact("transition", transition),
        ),
    )
