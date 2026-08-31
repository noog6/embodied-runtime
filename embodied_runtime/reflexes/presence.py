"""Deterministic reactions to semantic presence transitions."""

import logging

from embodied_runtime.events import EventBus, PresenceChanged, Subscription
from embodied_runtime.reflexes.base import SemanticBodyCapabilities

LOGGER = logging.getLogger(__name__)


class PresenceCenteringReflex:
    """Center the body whenever semantic presence appears."""

    identifier = "presence_centering"

    def __init__(self) -> None:
        self._subscription: Subscription[PresenceChanged] | None = None
        self._capabilities: SemanticBodyCapabilities | None = None

    async def start(
        self, events: EventBus, capabilities: SemanticBodyCapabilities
    ) -> None:
        if self._subscription is not None:
            raise RuntimeError("Presence centering reflex is already started")
        self._capabilities = capabilities
        self._subscription = events.subscribe(PresenceChanged, self._on_presence_changed)
        LOGGER.info("[REFLEX] name=%s status=ready", self.identifier)

    async def stop(self) -> None:
        subscription, self._subscription = self._subscription, None
        self._capabilities = None
        if subscription is not None:
            await subscription.close()

    async def _on_presence_changed(self, event: PresenceChanged) -> None:
        if not event.present:
            return
        capabilities = self._capabilities
        if capabilities is None:
            return
        try:
            await capabilities.set_body_orientation(
                yaw_degrees=0.0, pitch_degrees=0.0
            )
        except Exception:
            LOGGER.exception(
                "[REFLEX] name=%s action=center status=failed", self.identifier
            )
            return
        LOGGER.info(
            "[REFLEX] name=%s trigger=presence_present action=center status=applied",
            self.identifier,
        )
