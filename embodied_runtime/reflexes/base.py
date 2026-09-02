"""Minimal lifecycle contract for deterministic local reflexes."""

from typing import Protocol

from embodied_runtime.events import EventBus


class SemanticBodyCapabilities(Protocol):
    async def set_body_orientation(
        self, *, yaw_degrees: float, pitch_degrees: float, source: str = "application"
    ) -> object: ...


class Reflex(Protocol):
    identifier: str

    async def start(
        self, events: EventBus, capabilities: SemanticBodyCapabilities
    ) -> None: ...

    async def stop(self) -> None: ...
