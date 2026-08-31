"""Semantic presence transition events."""

from dataclasses import dataclass

from embodied_runtime.events.base import Event


@dataclass(frozen=True, slots=True, kw_only=True)
class PresenceChanged(Event):
    previous_present: bool | None
    present: bool
