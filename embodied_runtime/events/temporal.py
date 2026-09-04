"""Transient events produced by bounded temporal follow-ups."""

from dataclasses import dataclass

from embodied_runtime.events.base import Event


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalFollowupDue(Event):
    """One session-local follow-up reached its relative delay."""

    purpose: str
    delay_seconds: int
    # Runtime-internal identity fence. This reference is neither projected nor
    # serialized; semantic observation facts remain purpose and delay only.
    bound_goal: object
