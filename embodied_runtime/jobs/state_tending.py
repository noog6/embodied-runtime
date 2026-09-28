"""Bounded metadata for runtime-observable state-tending responsibilities."""

from dataclasses import dataclass

from .continuation import JobReadinessEventType
from .model import JobTriggerType


@dataclass(frozen=True, slots=True)
class StateTendingCondition:
    """A durable trigger and the event that reports its authoritative recovery."""

    trigger_type: JobTriggerType
    recovery_event: JobReadinessEventType


STATE_TENDING_CONDITIONS = {
    JobTriggerType.POWER_ATTENTION_REQUIRED: StateTendingCondition(
        JobTriggerType.POWER_ATTENTION_REQUIRED,
        JobReadinessEventType.POWER_RECOVERED,
    ),
    JobTriggerType.THERMAL_WARNING_RAISED: StateTendingCondition(
        JobTriggerType.THERMAL_WARNING_RAISED,
        JobReadinessEventType.THERMAL_WARNING_CLEARED,
    ),
    JobTriggerType.MEMORY_PRESSURE_RAISED: StateTendingCondition(
        JobTriggerType.MEMORY_PRESSURE_RAISED,
        JobReadinessEventType.MEMORY_PRESSURE_CLEARED,
    ),
}
