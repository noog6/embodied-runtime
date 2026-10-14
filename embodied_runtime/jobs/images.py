"""Bounded raster validation; encoded bytes are never transformed."""

from dataclasses import dataclass
import hashlib
import io
import struct
import warnings
import zlib

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096
MAX_IMAGE_PIXELS = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    media_type: str
    width: int
    height: int
    content_version: str


def is_image(data: bytes) -> bool:
    return data.startswith((b"\xff\xd8", b"\x89PNG"))


def validate_image(data: bytes) -> ImageMetadata:
    # Import here to keep the storage error vocabulary in one place.
    from .workspace import WorkspaceQuotaError, WorkspaceValidationError

    if type(data) is not bytes or not data:
        raise WorkspaceValidationError("invalid image")
    if len(data) > MAX_IMAGE_BYTES:
        raise WorkspaceQuotaError("image encoded bytes exceed limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=("JPEG", "PNG")) as image:
                width, height = image.size
                if (not 0 < width <= MAX_IMAGE_DIMENSION
                        or not 0 < height <= MAX_IMAGE_DIMENSION
                        or width * height > MAX_IMAGE_PIXELS):
                    raise WorkspaceQuotaError("image dimensions exceed limit")
                if getattr(image, "n_frames", 1) != 1:
                    raise WorkspaceValidationError("animated images are unsupported")
                media_type = {"JPEG": "image/jpeg", "PNG": "image/png"}[image.format]
                image.verify()
            with Image.open(io.BytesIO(data), formats=("JPEG", "PNG")) as image:
                image.load()
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
        raise WorkspaceQuotaError("image dimensions exceed limit") from error
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            EOFError, struct.error, zlib.error) as error:
        raise WorkspaceValidationError("invalid raster image") from error
    return ImageMetadata(media_type, width, height, hashlib.sha256(data).hexdigest())
