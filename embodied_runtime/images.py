"""Ordered provider-neutral image inputs and small-device selection budgets."""

from dataclasses import dataclass, field
from datetime import datetime
import json
import hashlib


@dataclass(frozen=True, slots=True)
class ImageLimits:
    max_images: int = 4
    max_total_bytes: int = 12 * 1024 * 1024
    max_total_pixels: int = 16 * 1024 * 1024

    def __post_init__(self):
        for name, ceiling in (("max_images", 16), ("max_total_bytes", 64 * 1024 * 1024),
                              ("max_total_pixels", 64 * 1024 * 1024)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(f"{name} must be an integer from 1 to {ceiling}")

    def validate(self, images, *, decode=False):
        if len(images) > self.max_images:
            raise ValueError(f"image count exceeds configured maximum ({self.max_images})")
        if sum(len(image.data) for image in images) > self.max_total_bytes:
            raise ValueError("aggregate image bytes exceed configured maximum")
        pixels = 0
        metadata = []
        for image in images:
            if (type(image.data) is not bytes or not 0 < len(image.data) <= 4 * 1024 * 1024
                    or image.media_type not in ("image/jpeg", "image/png", "image/webp")):
                raise ValueError("invalid or oversized image")
            if decode:
                from embodied_runtime.jobs.images import validate_image
                meta = (image.raster_metadata() if hasattr(image, "raster_metadata")
                        else validate_image(image.data, allow_webp=True))
                metadata.append(meta)
                if meta.media_type != image.media_type:
                    raise ValueError("image media type mismatch")
                width, height = meta.width, meta.height
            else:
                width, height = getattr(image, "width", 0), getattr(image, "height", 0)
            if (width < 0 or height < 0 or width > 4096 or height > 4096
                    or width * height > 4 * 1024 * 1024):
                raise ValueError("image dimensions exceed limit")
            pixels += width * height
        if pixels > self.max_total_pixels:
            raise ValueError("aggregate decoded image pixels exceed configured maximum")
        return tuple(metadata)


@dataclass(frozen=True, slots=True)
class ImageInput:
    data: bytes = field(repr=False)
    media_type: str
    width: int
    height: int
    reference: str
    label: str
    source: str
    captured_at: datetime | None = None
    received_at: datetime | None = None

    def __post_init__(self):
        for value in (self.captured_at, self.received_at):
            if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
                raise ValueError("image timestamps must be offset-aware")
        for name, limit in (("reference", 300), ("label", 300), ("source", 80)):
            value = getattr(self, name)
            if not isinstance(value, str) or not 0 < len(value) <= limit:
                raise ValueError(f"image {name} must be bounded text")

    def metadata(self):
        return {"image_ref": self.reference, "label": self.label, "source": self.source,
                "integrity_hash": hashlib.sha256(self.data).hexdigest(),
                "captured_at": self.captured_at.isoformat() if self.captured_at else None,
                "received_at": self.received_at.isoformat() if self.received_at else None}

    def description(self):
        return json.dumps(self.metadata(), ensure_ascii=True)


@dataclass(frozen=True, slots=True)
class ImageCollection:
    images: tuple[ImageInput, ...]

    def __post_init__(self):
        object.__setattr__(self, "images", tuple(self.images))
        if not self.images or not all(isinstance(image, ImageInput) for image in self.images):
            raise ValueError("image selection must be nonempty")


JOINT_IMAGE_INSTRUCTIONS = (
    "Analyze all selected images together in their supplied order. Associate observations "
    "with the exact image references and labels. Use visible evidence, distinguish visible "
    "differences from lighting, viewpoint, occlusion and camera changes. Capture time may "
    "be unknown; receipt time is not capture time. These are discrete images, not continuous "
    "observation. For an explicitly ordered sequence describe only suggested changes; do not "
    "claim certainty about actions or events between frames. State uncertainty. Be concise."
)
