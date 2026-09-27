"""Provider-neutral, request-scoped interaction image values."""

from dataclasses import dataclass


MAX_INTERACTION_IMAGE_BYTES = 4 * 1024 * 1024
SUPPORTED_INTERACTION_IMAGE_TYPES = frozenset({
    "image/jpeg", "image/png", "image/webp",
})


@dataclass(frozen=True, slots=True)
class ImageAttachment:
    """One in-memory image supplied as current interaction input."""

    media_type: str
    data: bytes

    def __post_init__(self) -> None:
        if self.media_type not in SUPPORTED_INTERACTION_IMAGE_TYPES:
            raise ValueError("unsupported interaction image media type")
        if not isinstance(self.data, bytes):
            raise TypeError("interaction image data must be bytes")
        if not 1 <= len(self.data) <= MAX_INTERACTION_IMAGE_BYTES:
            raise ValueError("interaction image data must contain between 1 byte and 4 MiB")
