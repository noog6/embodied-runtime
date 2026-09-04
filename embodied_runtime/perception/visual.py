"""Bounded interpretation of one transient camera frame."""

from abc import ABC, abstractmethod
import base64
from dataclasses import dataclass
import os
from typing import Any

from embodied_runtime.cognition.base import CognitionError, CognitionUnavailableError
from embodied_runtime.cognition.openai_responses import DEFAULT_MODEL
from embodied_runtime.sensing.camera import CameraFrame

MAX_CAMERA_FRAME_BYTES = 4 * 1024 * 1024
MAX_VISUAL_DESCRIPTION_CHARS = 2000
VISUAL_PROVIDER_INSTRUCTIONS = (
    "Analyze exactly this one current camera frame. Answer the supplied visual focus "
    "using visible evidence only. State uncertainty when ambiguous. Do not infer hidden "
    "objects or events. Be concise."
)


@dataclass(frozen=True, slots=True)
class VisualPerceptionResult:
    """One bounded, non-authoritative model interpretation."""

    focus: str
    description: str
    truncated: bool = False


class VisualPerceptionBackend(ABC):
    identifier: str

    @abstractmethod
    async def interpret(
        self, frame: CameraFrame, focus: str
    ) -> VisualPerceptionResult: ...


class OpenAIResponsesVisualPerceptionBackend(VisualPerceptionBackend):
    """Lazy OpenAI Responses adapter using an in-memory data URL."""

    identifier = "openai-responses"

    def __init__(self, *, model: str | None = None, client: Any = None) -> None:
        self.model = (
            model or os.environ.get("OPENAI_VISION_MODEL")
            or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
        )
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError as error:
            raise CognitionUnavailableError(
                "OpenAI visual perception is unavailable; install the 'openai' optional dependency"
            ) from error
        try:
            self._client = AsyncOpenAI()
        except Exception as error:
            raise CognitionUnavailableError(
                "OpenAI visual perception is unavailable; check OPENAI_API_KEY"
            ) from error
        return self._client

    async def interpret(self, frame: CameraFrame, focus: str) -> VisualPerceptionResult:
        encoded = base64.b64encode(frame.data).decode("ascii")
        image_url = f"data:{frame.media_type};base64,{encoded}"
        try:
            response = await self._get_client().responses.create(
                model=self.model,
                instructions=VISUAL_PROVIDER_INSTRUCTIONS,
                input=[{"role": "user", "content": [
                    {"type": "input_text", "text": focus},
                    {"type": "input_image", "image_url": image_url},
                ]}],
            )
            description = response.output_text.strip()
        except Exception as error:
            raise CognitionError("OpenAI visual perception request failed") from error
        if not description:
            raise CognitionError("OpenAI visual perception returned no description")
        truncated = len(description) > MAX_VISUAL_DESCRIPTION_CHARS
        return VisualPerceptionResult(
            focus, description[:MAX_VISUAL_DESCRIPTION_CHARS], truncated
        )
