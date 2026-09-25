"""Text cognition through the OpenAI Responses API."""

from collections.abc import Sequence
from typing import Any
import logging
import os
import time

from embodied_runtime.cognition.base import (
    CognitionError,
    CognitionToolCall,
    CognitionToolDefinition,
    CognitionToolExecutor,
    CognitionUnavailableError,
    InstructionsProvider,
    TextCognitionBackend,
)
from embodied_runtime.observability import RunObservability

DEFAULT_MODEL = "gpt-5.6-luna"
PREWARM_INPUT = "Reply ready."
LOGGER = logging.getLogger(__name__)


class OpenAIResponsesBackend(TextCognitionBackend):
    """A lazy, asynchronous OpenAI Responses text adapter."""

    identifier = "openai-responses"

    def __init__(self, *, model: str | None = None, client: Any = None,
                 observability: RunObservability | None = None) -> None:
        self.model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
        self._client = client
        self._client_init_measured = client is not None
        self._provider_request_ordinal = 0
        self._preparation_attempted = False
        self.observability = observability

    async def prepare(self) -> None:
        """Initialize the client and make one tool-free provider prewarm request."""
        if self._preparation_attempted:
            return
        self._preparation_attempted = True
        self._get_client()
        try:
            await self._provider_request(
                "prewarm", {"model": self.model, "input": PREWARM_INPUT},
                message_chars=len(PREWARM_INPUT), instruction_chars=0, tools=0,
            )
        except CognitionError:
            raise
        except Exception as error:
            raise CognitionError("OpenAI Responses preparation failed") from error

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        started = time.perf_counter()
        try:
            from openai import AsyncOpenAI
        except ImportError as error:
            self._log_client_init("failed", started)
            raise CognitionUnavailableError(
                "OpenAI cognition is unavailable; install the 'openai' optional dependency"
            ) from error
        try:
            self._client = AsyncOpenAI()
        except Exception as error:
            self._log_client_init("failed", started)
            raise CognitionUnavailableError(
                "OpenAI cognition is unavailable; check OPENAI_API_KEY"
            ) from error
        self._log_client_init("completed", started)
        return self._client

    def _log_client_init(self, status: str, started: float) -> None:
        if self._client_init_measured:
            return
        self._client_init_measured = True
        LOGGER.info(
            "[COGNITION] backend=%s component=client_init status=%s cold=true "
            "duration_ms=%s",
            self.identifier, status, int((time.perf_counter() - started) * 1_000),
        )

    async def _provider_request(
        self, kind: str, arguments: dict[str, Any], *, message_chars: int | None,
        instruction_chars: int, tools: int,
    ) -> Any:
        """Time exactly one outbound Responses call and log bounded metadata."""
        client = self._get_client()
        self._provider_request_ordinal += 1
        ordinal = self._provider_request_ordinal
        cold = ordinal == 1
        started = time.perf_counter()
        try:
            response = await client.responses.create(**arguments)
        except Exception as error:
            self._log_provider_request(
                kind, ordinal, cold, "failed", started, message_chars,
                instruction_chars, tools,
            )
            if self.observability is not None:
                self.observability.provider_failed(
                    self.identifier, self.model, kind,
                    duration_ms=int((time.perf_counter() - started) * 1_000),
                    error=type(error).__name__,
                )
            raise
        self._log_provider_request(
            kind, ordinal, cold, "completed", started, message_chars,
            instruction_chars, tools, response,
        )
        if self.observability is not None:
            usage = getattr(response, "usage", None)
            details = getattr(usage, "input_tokens_details", None)
            def token(name: str, owner: Any = usage) -> int:
                value = getattr(owner, name, 0) if owner is not None else 0
                return value if isinstance(value, int) and not isinstance(value, bool) else 0
            self.observability.provider_completed(
                self.identifier, self.model, kind,
                input_tokens=token("input_tokens"),
                cached_input_tokens=token("cached_tokens", details),
                cache_write_tokens=token("cache_write_tokens", details),
                output_tokens=token("output_tokens"),
                total_tokens=token("total_tokens"),
                duration_ms=int((time.perf_counter() - started) * 1_000),
                usage_available=(usage is not None and all(
                    isinstance(getattr(usage, name, None), int)
                    and not isinstance(getattr(usage, name, None), bool)
                    for name in ("input_tokens", "output_tokens", "total_tokens")
                )),
            )
        return response

    def _log_provider_request(
        self, kind: str, ordinal: int, cold: bool, status: str, started: float,
        message_chars: int | None, instruction_chars: int, tools: int,
        response: Any = None,
    ) -> None:
        fields = [
            f"[COGNITION] backend={self.identifier}",
            f"provider_request={kind}", f"ordinal={ordinal}",
            f"cold={str(cold).lower()}", f"status={status}",
            f"duration_ms={int((time.perf_counter() - started) * 1_000)}",
        ]
        if message_chars is not None:
            fields.append(f"message_chars={message_chars}")
        fields.extend((f"instruction_chars={instruction_chars}", f"tools={tools}"))
        if response is not None:
            fields.extend(self._usage_fields(response))
        LOGGER.info(" ".join(fields))

    @staticmethod
    def _usage_fields(response: Any) -> list[str]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return []
        fields = []
        for public_name in ("input_tokens", "output_tokens", "total_tokens"):
            value = getattr(usage, public_name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                fields.append(f"{public_name}={value}")
        details = getattr(usage, "input_tokens_details", None)
        cached = getattr(details, "cached_tokens", None)
        if isinstance(cached, int) and not isinstance(cached, bool):
            fields.append(f"cached_input_tokens={cached}")
        cache_write = getattr(details, "cache_write_tokens", None)
        if isinstance(cache_write, int) and not isinstance(cache_write, bool):
            fields.append(f"cache_write_tokens={cache_write}")
        return fields

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
            response = await self._provider_request(
                "initial", arguments, message_chars=len(message),
                instruction_chars=len(instructions or ""), tools=len(tools),
            )
            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:
                return response.output_text
            if len(calls) != 1:
                raise CognitionError("Provider requested multiple cognition tools")
            call = calls[0]
            result = await tool_executor(
                CognitionToolCall(name=call.name, arguments=call.arguments)
            )
            final_arguments = dict(
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
            final = await self._provider_request(
                "continuation", final_arguments, message_chars=None,
                instruction_chars=len(final_arguments["instructions"]), tools=0,
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
        properties = tool.parameters.get("properties")
        required = tool.parameters.get("required")
        if (
            tool.parameters.get("type") != "object"
            or not isinstance(properties, dict)
            or not isinstance(required, list)
            or set(properties) != set(required)
            or tool.parameters.get("additionalProperties") is not False
        ):
            raise CognitionError(
                f"Strict cognition tool schema is incompatible: {tool.name}"
            )
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
            "strict": True,
        }
