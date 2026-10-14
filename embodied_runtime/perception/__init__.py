"""Provider-neutral semantic perception contracts."""

from embodied_runtime.perception.visual import (
    MAX_CAMERA_FRAME_BYTES, MAX_VISUAL_DESCRIPTION_CHARS, ImageSnapshot,
    OpenAIResponsesVisualPerceptionBackend, OpenAICompatibleVisualPerceptionBackend,
    OrderedVisualPerceptionBackend, VisualPerceptionBackend, VisualPerceptionError, VisualPerceptionResult,
)

__all__ = [
    "MAX_CAMERA_FRAME_BYTES", "MAX_VISUAL_DESCRIPTION_CHARS", "ImageSnapshot",
    "OpenAIResponsesVisualPerceptionBackend", "VisualPerceptionBackend",
    "OpenAICompatibleVisualPerceptionBackend", "OrderedVisualPerceptionBackend",
    "VisualPerceptionError", "VisualPerceptionResult",
]
