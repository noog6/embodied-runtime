"""Bounded request-local cognition through OpenAI-compatible Chat Completions."""

import asyncio
import base64
from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

from embodied_runtime.attachments import ImageAttachment
from embodied_runtime.images import ImageLimits, JOINT_IMAGE_INSTRUCTIONS
from embodied_runtime.config import OpenAICompatibleConfig, validate_compatible_config
from embodied_runtime.observability import RunObservability
from embodied_runtime.monetary import openrouter_cost
from .base import (
    CognitionError, CognitionToolCall, CognitionToolDefinition,
    CognitionToolExecutor, CognitionUnavailableError, InstructionsProvider,
    TextCognitionBackend,
)
from .provider_diagnostics import endpoint_identity, failure_category

LOGGER = logging.getLogger(__name__)
SDK_MAX_RETRIES = 0
POST_TOOL_RESPONSE_INSTRUCTIONS = (
    "Current response stage: post-tool final prose (governs this response).\n"
    "The preceding tool call has completed. No further tool calls are permitted in this response. "
    "Return a brief textual assessment based on the tool result and current runtime context, "
    "preserving any uncertainty or failure. Do not claim actions beyond what the result confirms. "
    "Any further acquisition or action requires a separate runtime-authorized decision; "
    "do not request or execute it now. This final prose ends only this adapter response; "
    "it does not itself complete the overall Job."
)


def _field(owner: Any, name: str, default: Any = None) -> Any:
    return owner.get(name, default) if isinstance(owner, Mapping) else getattr(owner, name, default)


def _token(owner: Any, name: str) -> int | None:
    value = _field(owner, name)
    return value if type(value) is int and value >= 0 else None


class OpenAICompatibleBackend(TextCognitionBackend):
    """One initial call, at most one runtime tool execution, one final call."""

    identifier = "openai-compatible"
    protocol = "chat-completions"

    def __init__(self, *, config: OpenAICompatibleConfig, client: Any = None,
                 observability: RunObservability | None = None) -> None:
        validate_compatible_config(config)
        self.config = config
        self.model = config.model
        self.endpoint = endpoint_identity(config.base_url)
        self.supports_image_input = config.image_input
        self._openrouter = urlsplit(config.base_url).hostname == "openrouter.ai"
        self._client = client
        self._preparation_attempted = False
        self.observability = observability

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError:
                raise CognitionUnavailableError(
                    "Compatible cognition requires the 'openai' optional dependency",
                    category="unavailable",
                ) from None
            key = os.environ.get(self.config.api_key_env)
            if not key or not key.strip():
                raise CognitionUnavailableError(
                    f"Compatible cognition credential environment variable {self.config.api_key_env} is missing or empty",
                    category="authentication",
                )
            try:
                self._client = AsyncOpenAI(
                    api_key=key, base_url=self.config.base_url,
                    timeout=self.config.request_timeout_seconds,
                    max_retries=SDK_MAX_RETRIES,
                )
            except Exception:
                raise CognitionUnavailableError(
                    "Compatible cognition client initialization failed", category="unavailable",
                ) from None
        return self._client

    async def prepare(self) -> None:
        """Preserve startup's one prewarm before publishing runtime readiness."""
        if self._preparation_attempted:
            return
        self._preparation_attempted = True
        await self._request("prewarm", [{"role": "user", "content": "Reply ready."}])

    async def _request(self, stage: str, messages: list[dict[str, Any]], *,
                       tools: list[dict[str, Any]] | None = None,
                       final: bool = False) -> tuple[str | None, dict[str, Any] | None]:
        arguments: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False}
        if tools:
            # OpenRouter validates schemas in both stages, even with tools disabled.
            arguments.update(tools=tools, tool_choice="none" if final else "auto")
            if self.config.parallel_tool_calls_mode == "disable":
                arguments["parallel_tool_calls"] = False
        else:
            arguments["tool_choice"] = "none"
        if self._openrouter:
            arguments["extra_body"] = {"provider": {"require_parameters": True, "allow_fallbacks": False}}
        # Client setup failures are not attempted outbound provider calls.
        client = self._get_client()
        started = time.perf_counter()
        response = None
        caught = None
        try:
            # A wall deadline bounds the entire SDK call, including a stalled fake
            # or endpoint. Zero SDK retries prevents hidden interaction replay.
            async with asyncio.timeout(self.config.request_timeout_seconds):
                response = await client.chat.completions.create(**arguments)
            return self._decode(response, allow_tool=bool(tools) and not final)
        except asyncio.CancelledError as error:
            caught = error
            raise
        except CognitionError as error:
            caught = error
            if final:
                self._log_rejected_final_tool_calls(response, tools or [])
            raise
        except Exception as error:
            caught = error
            # Provider exception messages/bodies/URLs can contain credentials.
            raise CognitionError(
                f"Compatible cognition {stage} failed ({failure_category(error)})",
                category=failure_category(error),
            ) from None
        finally:
            duration = int((time.perf_counter() - started) * 1_000)
            category = failure_category(caught) if caught is not None else None
            status = "cancelled" if category == "cancelled" else "failed" if caught is not None else "completed"
            usage = _field(response, "usage")
            counts = {name: _token(usage, source) for name, source in (
                ("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
                ("total_tokens", "total_tokens"),
            )}
            available = all(value is not None for value in counts.values())
            details = _field(usage, "prompt_tokens_details")
            cached = _token(details, "cached_tokens")
            counts["cached_input_tokens"] = cached
            counts["cache_write_tokens"] = _token(details, "cache_write_tokens")
            counts["reasoning_tokens"] = _token(_field(usage, "completion_tokens_details"), "reasoning_tokens")
            returned = _field(response, "model")
            returned = returned if isinstance(returned, str) else None
            LOGGER.info(
                "[COGNITION] backend=%s protocol=%s endpoint=%s model=%s returned_model=%s "
                "provider_request=%s status=%s duration_ms=%s usage_available=%s "
                "input_tokens=%s output_tokens=%s total_tokens=%s failure_category=%s",
                self.identifier, self.protocol, self.endpoint, self.model, returned,
                stage, status, duration, available, counts["input_tokens"],
                counts["output_tokens"], counts["total_tokens"], category,
            )
            if self.observability is not None:
                self.observability.provider_call(
                    self.identifier, self.model, stage, duration_ms=duration,
                    status=status, error=category, failure_category=category,
                    usage=counts if response is not None else None,
                    protocol=self.protocol, endpoint=self.endpoint, returned_model=returned,
                    reported_cost_usd=openrouter_cost(response, base_url=self.config.base_url),
                )

    @staticmethod
    def _log_rejected_final_tool_calls(response: Any, tools: list[dict[str, Any]]) -> None:
        """Best-effort structural evidence; never emit provider content or mask rejection."""
        try:
            choices = _field(response, "choices")
            if not isinstance(choices, list) or len(choices) != 1:
                return
            choice = choices[0]
            message = _field(choice, "message")
            calls = _field(message, "tool_calls")
            if not isinstance(calls, list) or not calls:
                return
            finish = _field(choice, "finish_reason")
            if not isinstance(finish, str) or finish not in (
                "stop", "tool_calls", "length", "content_filter", "function_call",
            ):
                finish = "<invalid>"
            offered = {tool["function"]["name"] for tool in tools}
            names = []
            # Bound both inspection and output, regardless of the rejected call count.
            for call in calls[:8]:
                name = _field(_field(call, "function"), "name")
                names.append(name if (
                    _field(call, "type") == "function" and isinstance(name, str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) and name in offered
                ) else "<invalid-or-unoffered>")
            content = _field(message, "content")
            LOGGER.warning(
                "[COGNITION] provider_request=continuation rejected_final_tool_calls "
                "finish_reason=%s tool_call_count=%s tool_names=%s nonempty_text=%s",
                finish, len(calls), names, isinstance(content, str) and bool(content.strip()),
            )
        except Exception:
            # Diagnostics (including logging handlers) cannot replace the original error.
            pass

    @staticmethod
    def _decode(response: Any, *, allow_tool: bool) -> tuple[str | None, dict[str, Any] | None]:
        def reject(category: str, message: str) -> None:
            raise CognitionError(message, category=category)

        choices = _field(response, "choices")
        if not isinstance(choices, list) or len(choices) != 1:
            reject("malformed_response", "Compatible cognition requires exactly one response choice")
        choice = choices[0]
        message = _field(choice, "message")
        if _field(message, "role") != "assistant":
            reject("malformed_response", "Compatible cognition received an invalid assistant message")
        finish = _field(choice, "finish_reason")
        if finish == "length":
            reject("truncated", "Compatible cognition response was truncated")
        if finish == "content_filter" or _field(message, "refusal"):
            reject("refused", "Compatible cognition response was refused")
        if finish not in ("stop", "tool_calls") or _field(message, "function_call") is not None:
            reject("malformed_response", "Compatible cognition received an unsupported response structure")
        content = _field(message, "content")
        if content is not None and not isinstance(content, str):
            reject("malformed_response", "Compatible cognition requires plain-text assistant content")
        calls = _field(message, "tool_calls")
        if calls is not None and not isinstance(calls, list):
            reject("malformed_tool_call", "Compatible cognition received invalid tool calls")
        if calls:
            if not allow_tool:
                reject("unexpected_tool_call", "Provider requested an additional or unoffered cognition tool")
            if len(calls) != 1:
                reject("multiple_tool_calls", "Provider requested multiple cognition tools")
            call = calls[0]
            function = _field(call, "function")
            call_id = _field(call, "id")
            name = _field(function, "name")
            arguments = _field(function, "arguments")
            if (finish != "tool_calls" or _field(call, "type") != "function"
                    or not isinstance(call_id, str) or not call_id.strip()
                    or not isinstance(name, str) or not name.strip()
                    or not isinstance(arguments, str)):
                reject("malformed_tool_call", "Compatible cognition received a malformed function call")
            try:
                def invalid_constant(_value):
                    raise ValueError("invalid JSON constant")
                decoded = json.loads(arguments, parse_constant=invalid_constant)
            except (ValueError, TypeError):
                decoded = None
            if not isinstance(decoded, dict):
                reject("malformed_tool_call", "Compatible cognition tool arguments must be a JSON object")
            assistant = {"role": "assistant", "content": content, "tool_calls": [{
                "id": call_id, "type": "function", "function": {"name": name, "arguments": arguments},
            }]}
            # OpenRouter reasoning blocks (including opaque signatures) must be
            # echoed unchanged. They are transport state, never runtime grounding.
            reasoning = _field(message, "reasoning_details")
            if reasoning is not None:
                try:
                    if not isinstance(reasoning, list) or not all(isinstance(item, dict) for item in reasoning):
                        raise ValueError
                    json.dumps(reasoning, allow_nan=False)
                except (ValueError, TypeError):
                    reject("malformed_response", "Compatible cognition received invalid continuation metadata")
                assistant["reasoning_details"] = deepcopy(reasoning)
            return None, assistant
        if finish != "stop":
            reject("malformed_tool_call", "Compatible cognition tool finish reason has no tool call")
        if not content or not content.strip():
            reject("empty_response", "Compatible cognition returned an empty response")
        return content, None

    async def respond(self, message: str, *, instructions: str | None = None,
                      tools: Sequence[CognitionToolDefinition] = (),
                      tool_executor: CognitionToolExecutor | None = None,
                      refreshed_instructions: InstructionsProvider | None = None,
                      image_attachments: Sequence[ImageAttachment] = ()) -> str:
        if image_attachments and not self.supports_image_input:
            raise CognitionError("Configured compatible model does not support image input", category="unsupported_capability")
        getattr(self, "image_limits", ImageLimits()).validate(image_attachments, decode=True)
        if tools and (tool_executor is None or refreshed_instructions is None):
            raise CognitionError("Cognition tools require runtime execution and grounding")
        content: Any = message
        if image_attachments:
            content = [{"type": "text", "text": message}] if message else []
            if len(image_attachments) > 1:
                content.append({"type": "text", "text": JOINT_IMAGE_INSTRUCTIONS})
            for index, attachment in enumerate(image_attachments, 1):
                if attachment.reference or attachment.label:
                    content.append({"type": "text", "text": f"Image {index}: {attachment.reference or index}; label={attachment.label or index}; source={attachment.source}; received_at={attachment.received_at}; capture time unknown"})
                encoded = base64.b64encode(attachment.data).decode("ascii")
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:{attachment.media_type};base64,{encoded}", "detail": "auto",
                }})
        original = {"role": "user", "content": content}
        messages = ([{"role": "system", "content": instructions}] if instructions is not None else []) + [original]
        provider_tools = [{"type": "function", "function": {
            "name": tool.name, "description": tool.description, "parameters": dict(tool.parameters),
        }} for tool in tools]
        text, assistant = await self._request("initial", messages, tools=provider_tools)
        if assistant is None:
            return text
        call = assistant["tool_calls"][0]
        result = await tool_executor(CognitionToolCall(
            call["function"]["name"], call["function"]["arguments"],
        ))
        # Replace stale grounding and scope the current response to final prose.
        # Only this request's user message, assistant call/metadata, and result continue.
        continuation = [
            {"role": "system", "content": refreshed_instructions() + "\n\n" + POST_TOOL_RESPONSE_INSTRUCTIONS}, original,
            assistant, {"role": "tool", "tool_call_id": call["id"], "content": result.output},
        ]
        text, _ = await self._request("continuation", continuation, tools=provider_tools, final=True)
        return text
