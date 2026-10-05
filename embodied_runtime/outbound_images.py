"""Provider-neutral values for explicitly requested outbound images."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from embodied_runtime.interaction import InteractionChannel


MAX_OUTBOUND_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_CAPTION_CHARS = 320


@dataclass(frozen=True, slots=True)
class RetainedImage:
    """Runtime-owned bytes; only ``reference`` and metadata are model-visible."""

    reference: str
    episode_id: int
    data: bytes
    media_type: str
    width: int
    height: int
    source: str
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class OutboundImage:
    """Exact retained image passed across a provider-neutral delivery boundary."""

    data: bytes
    media_type: str
    caption: str


@dataclass(frozen=True, slots=True)
class ImageDeliveryEvidence:
    """Provider submission evidence, explicitly distinct from handset receipt."""

    status: str
    provider_reference: str | None = None
    provider_status: str | None = None


class OperatorImageSink(Protocol):
    @property
    def channel(self) -> InteractionChannel: ...

    @property
    def image_delivery_available(self) -> bool: ...

    async def deliver_image(self, image: OutboundImage) -> ImageDeliveryEvidence: ...
