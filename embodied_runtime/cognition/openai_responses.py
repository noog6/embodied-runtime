"""Text cognition through the OpenAI Responses API."""

from typing import Any
import os

from embodied_runtime.cognition.base import CognitionError, CognitionUnavailableError

DEFAULT_MODEL = "gpt-5.6-luna"


class OpenAIResponsesBackend:
    """A lazy, asynchronous OpenAI Responses text adapter."""

    identifier = "openai-responses"

    def __init__(self, *, model: str | None = None, client: Any = None) -> None:
        self.model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError as error:
            raise CognitionUnavailableError(
                "OpenAI cognition is unavailable; install the 'openai' optional dependency"
            ) from error
        try:
            self._client = AsyncOpenAI()
        except Exception as error:
            raise CognitionUnavailableError(
                "OpenAI cognition is unavailable; check OPENAI_API_KEY"
            ) from error
        return self._client

    async def respond(
        self, message: str, *, instructions: str | None = None
    ) -> str:
        arguments = {"model": self.model, "input": message}
        if instructions is not None:
            arguments["instructions"] = instructions
        try:
            response = await self._get_client().responses.create(**arguments)
            return response.output_text
        except CognitionError:
            raise
        except Exception as error:
            raise CognitionError("OpenAI Responses request failed") from error
