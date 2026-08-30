"""Events announcing application lifecycle transitions."""

from dataclasses import dataclass

from embodied_runtime.events.base import Event


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationStarted(Event):
    pass
