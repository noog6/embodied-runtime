"""Text cognition through the OpenAI Responses API."""

from collections.abc import Sequence
import asyncio
import base64
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
from embodied_runtime.attachments import ImageAttachment
from embodied_runtime.cognition.provider_diagnostics import endpoint_identity, failure_category

DEFAULT_MODEL = "gpt-5.6-luna"
PREWARM_INPUT = "Reply ready."
LOGGER = logging.getLogger(__name__)


class OpenAIResponsesBackend(TextCognitionBackend):
    """A lazy, asynchronous OpenAI Responses text adapter."""

    identifier = "openai-responses"
    protocol = "responses"
    supports_image_input = True

    def __init__(self, *, model: str | None = None, client: Any = None,
                 observability: RunObservability | None = None) -> None:
        self.model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
        self.endpoint = endpoint_identity(os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1")
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
            raise CognitionError("OpenAI Responses preparation failed", category=failure_category(error)) from None

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
            ) from None
        try:
            self._client = AsyncOpenAI()
        except Exception as error:
            self._log_client_init("failed", started)
            raise CognitionUnavailableError(
                "OpenAI cognition is unavailable; check OPENAI_API_KEY"
            ) from None
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
        except (Exception, asyncio.CancelledError) as error:
            status = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
            self._log_provider_request(
                kind, ordinal, cold, status, started, message_chars,
                instruction_chars, tools,
            )
            if self.observability is not None:
                self.observability.provider_failed(
                    self.identifier, self.model, kind,
                    duration_ms=int((time.perf_counter() - started) * 1_000),
                    error=type(error).__name__,
                    protocol=self.protocol, endpoint=self.endpoint,
                    failure_category=failure_category(error),
                    status=status,
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
                return value if type(value) is int and value >= 0 else 0
            raw_usage = None if usage is None else {
                name: value if type(value := getattr(usage, name, None)) is int and value >= 0 else None
                for name in ("input_tokens", "output_tokens", "total_tokens")
            }
            if raw_usage is not None:
                raw_usage.update(cached_input_tokens=token("cached_tokens", details),
                                 cache_write_tokens=token("cache_write_tokens", details))
            self.observability.provider_completed(
                self.identifier, self.model, kind,
                input_tokens=token("input_tokens"),
                cached_input_tokens=token("cached_tokens", details),
                cache_write_tokens=token("cache_write_tokens", details),
                output_tokens=token("output_tokens"),
                total_tokens=token("total_tokens"),
                duration_ms=int((time.perf_counter() - started) * 1_000),
                usage_available=self._usage_available(response),
                protocol=self.protocol, endpoint=self.endpoint,
                returned_model=getattr(response, "model", None),
                raw_usage=raw_usage,
            )
        return response

    def _log_provider_request(
        self, kind: str, ordinal: int, cold: bool, status: str, started: float,
        message_chars: int | None, instruction_chars: int, tools: int,
        response: Any = None,
    ) -> None:
        fields = [
            f"[COGNITION] backend={self.identifier}",
            f"protocol={self.protocol}", f"endpoint={self.endpoint}",
            f"model={self.model}",
            f"provider_request={kind}", f"ordinal={ordinal}",
            f"cold={str(cold).lower()}", f"status={status}",
            f"duration_ms={int((time.perf_counter() - started) * 1_000)}",
        ]
        if message_chars is not None:
            fields.append(f"message_chars={message_chars}")
        fields.extend((f"instruction_chars={instruction_chars}", f"tools={tools}"))
        if response is not None:
            fields.append(f"returned_model={getattr(response, 'model', None)}")
            fields.append(f"usage_available={str(self._usage_available(response)).lower()}")
            fields.extend(self._usage_fields(response))
        LOGGER.info(" ".join(fields))

    @staticmethod
    def _usage_available(response: Any) -> bool:
        usage = getattr(response, "usage", None)
        return usage is not None and all(
            type(value := getattr(usage, name, None)) is int and value >= 0
            for name in ("input_tokens", "output_tokens", "total_tokens")
        )

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
        image_attachments: Sequence[ImageAttachment] = (),
    ) -> str:
        if len(image_attachments) > 1:
            raise CognitionError("OpenAI Responses permits at most one image attachment")
        request_input: Any = message
        if image_attachments:
            content: list[dict[str, str]] = []
            if message:
                content.append({"type": "input_text", "text": message})
            for attachment in image_attachments:
                encoded = base64.b64encode(attachment.data).decode("ascii")
                content.append({
                    "type": "input_image",
                    "image_url": f"data:{attachment.media_type};base64,{encoded}",
                    "detail": "auto",
                })
            request_input = [{"role": "user", "content": content}]
        arguments = {"model": self.model, "input": request_input}
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
            raise CognitionError("OpenAI Responses request failed", category=failure_category(error)) from None

    @staticmethod
    def _provider_tool(tool: CognitionToolDefinition) -> dict[str, Any]:
        properties = tool.parameters.get("properties")
        required = tool.parameters.get("required")
        if (
            tool.parameters.get("type") != "object"
            or not isinstance(properties, dict)
            or not isinstance(required, list)
            or not set(required) <= set(properties)
            or tool.parameters.get("additionalProperties") is not False
        ):
            raise CognitionError(
                f"Strict cognition tool schema is incompatible: {tool.name}"
            )
        optional = set(properties) - set(required)
        if any("null" not in definition.get("type", ())
               for name, definition in properties.items() if name in optional):
            raise CognitionError(
                f"Strict cognition tool optional fields must accept null: {tool.name}"
            )
        # OpenAI strict schemas require every property in ``required``. Runtime tool
        # definitions may nevertheless express genuinely optional nullable fields;
        # normalize those only at the provider boundary while the local executor
        # continues to accept omission as well as explicit null.
        parameters = dict(tool.parameters)
        parameters["required"] = list(properties)
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": parameters,
            "strict": True,
        }
