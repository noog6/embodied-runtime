"""Provider-neutral semantic perception contracts."""

from embodied_runtime.perception.visual import (
    MAX_CAMERA_FRAME_BYTES, MAX_VISUAL_DESCRIPTION_CHARS,
    OpenAIResponsesVisualPerceptionBackend, VisualPerceptionBackend,
    VisualPerceptionResult,
)

__all__ = [
    "MAX_CAMERA_FRAME_BYTES", "MAX_VISUAL_DESCRIPTION_CHARS",
    "OpenAIResponsesVisualPerceptionBackend", "VisualPerceptionBackend",
    "VisualPerceptionResult",
]
