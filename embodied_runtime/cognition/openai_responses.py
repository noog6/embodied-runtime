"""Text cognition through the OpenAI Responses API."""

from collections.abc import Sequence
from typing import Any
import os

from embodied_runtime.cognition.base import (
    CognitionError,
    CognitionToolCall,
    CognitionToolDefinition,
    CognitionToolExecutor,
    CognitionUnavailableError,
    InstructionsProvider,
)

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
        self,
        message: str,
        *,
        instructions: str | None = None,
        tools: Sequence[CognitionToolDefinition] = (),
        tool_executor: CognitionToolExecutor | None = None,
        refreshed_instructions: InstructionsProvider | None = None,
    ) -> str:
        arguments = {"model": self.model, "input": message}
        if instructions is not None:
            arguments["instructions"] = instructions
        if tools:
            if tool_executor is None or refreshed_instructions is None:
                raise CognitionError(
                    "Cognition tools require runtime execution and grounding"
                )
            arguments.update(
                tools=[self._provider_tool(tool) for tool in tools],
                tool_choice="auto",
                parallel_tool_calls=False,
            )
        try:
            response = await self._get_client().responses.create(**arguments)
            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:
                return response.output_text
            if len(calls) != 1:
                raise CognitionError("Provider requested multiple cognition tools")
            call = calls[0]
            result = await tool_executor(
                CognitionToolCall(name=call.name, arguments=call.arguments)
            )
            final = await self._get_client().responses.create(
                model=self.model,
                previous_response_id=response.id,
                input=[{
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": result.output,
                }],
                instructions=refreshed_instructions(),
                tool_choice="none",
            )
            if any(item.type == "function_call" for item in final.output):
                raise CognitionError("Provider requested an additional cognition tool")
            return final.output_text
        except CognitionError:
            raise
        except Exception as error:
            raise CognitionError("OpenAI Responses request failed") from error

    @staticmethod
    def _provider_tool(tool: CognitionToolDefinition) -> dict[str, Any]:
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
            "strict": True,
        }
