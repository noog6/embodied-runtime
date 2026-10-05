"""Provider-neutral values for bounded, operator-requested image delivery."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from embodied_runtime.interaction import InteractionChannel

MAX_OUTBOUND_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_CAPTION_CHARS = 320


@dataclass(frozen=True, slots=True)
class RetainedImage:
    """Runtime-owned immutable image; bytes are never projected to cognition."""

    data: bytes
    media_type: str
    width: int
    height: int
    source: str
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class ImageDeliveryEvidence:
    """Bounded provider submission evidence, not handset-delivery evidence."""

    status: str
    provider_status: str | None = None
    provider_reference: str | None = None


class OperatorImageSink(Protocol):
    @property
    def channel(self) -> InteractionChannel: ...

    @property
    def image_delivery_available(self) -> bool: ...

    async def deliver_image(
        self, image: RetainedImage, caption: str | None,
    ) -> ImageDeliveryEvidence: ...
