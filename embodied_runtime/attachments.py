"""Provider-neutral, request-scoped interaction image values."""

from dataclasses import dataclass, field, replace
from datetime import datetime


MAX_INTERACTION_IMAGE_BYTES = 4 * 1024 * 1024
SUPPORTED_INTERACTION_IMAGE_TYPES = frozenset({
    "image/jpeg", "image/png", "image/webp",
})


@dataclass(frozen=True, slots=True)
class ImageAttachment:
    """One in-memory image supplied as current interaction input."""

    media_type: str
    data: bytes = field(repr=False)
    reference: str | None = None
    label: str | None = None
    source: str = "operator_attachment"
    received_at: datetime | None = None
    source_message_id: str | None = None
    attachment_index: int = 0
    _metadata: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name, limit in (("reference", 100), ("label", 120), ("source", 80), ("source_message_id", 128)):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not 0 < len(value) <= limit
                                      or any(ord(char) < 32 for char in value)):
                raise ValueError(f"interaction image {name} must be bounded plain text")
        if type(self.attachment_index) is not int or not 0 <= self.attachment_index < 16:
            raise ValueError("interaction image attachment index must be 0..15")
        if self.received_at is not None and (not isinstance(self.received_at, datetime)
                                            or self.received_at.utcoffset() is None):
            raise ValueError("interaction image receipt time must be offset-aware")
        if self.media_type not in SUPPORTED_INTERACTION_IMAGE_TYPES:
            raise ValueError("unsupported interaction image media type")
        if not isinstance(self.data, bytes):
            raise TypeError("interaction image data must be bytes")
        if not 1 <= len(self.data) <= MAX_INTERACTION_IMAGE_BYTES:
            raise ValueError("interaction image data must contain between 1 byte and 4 MiB")

    def raster_metadata(self):
        """Decode immutable bytes once per in-process attachment, never cache pixels."""
        if self._metadata is None:
            from embodied_runtime.jobs.images import validate_image
            metadata = validate_image(self.data, allow_webp=True)
            if metadata.media_type != self.media_type:
                raise ValueError("image media type mismatch")
            object.__setattr__(self, "_metadata", metadata)
        return self._metadata

    def with_context(self, **values):
        result = replace(self, **values)
        if result.data is self.data and result.media_type == self.media_type:
            object.__setattr__(result, "_metadata", self._metadata)
        return result
