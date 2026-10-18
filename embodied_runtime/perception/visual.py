"""Bounded interpretation of ordered images, with immutable fallback inputs."""

from abc import ABC, abstractmethod
import asyncio
import base64
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
import logging
from json import JSONDecodeError
import os
import time
import unicodedata
from typing import Any
from urllib.parse import urlsplit

from embodied_runtime.cognition.base import CognitionError
from embodied_runtime.cognition.openai_responses import DEFAULT_MODEL
from embodied_runtime.cognition.provider_diagnostics import (
    endpoint_identity, failure_category, provider_http_status,
)
from embodied_runtime.config import (
    ConfigurationError, MAX_VISION_BACKENDS, VisionBackendConfig,
    VisionConfiguration, validate_vision_backend_config, validate_vision_configuration,
)
from embodied_runtime.observability import RunObservability
from embodied_runtime.sensing.camera import CameraFrame
from embodied_runtime.images import ImageCollection, ImageLimits, JOINT_IMAGE_INSTRUCTIONS

LOGGER = logging.getLogger(__name__)
MAX_CAMERA_FRAME_BYTES = 4 * 1024 * 1024
MAX_VISUAL_DESCRIPTION_CHARS = 2000
MAX_VISUAL_OUTPUT_TOKENS = 4096
VISUAL_PROVIDER_INSTRUCTIONS = (
    "Analyze exactly this one current camera frame. Answer the supplied visual focus "
    "using visible evidence only. State uncertainty when ambiguous. Do not infer hidden "
    "objects or events. Be concise."
)
FALLBACK_CATEGORIES = frozenset({
    "timeout", "connection", "rate_limit", "provider_unavailable", "unavailable",
    "missing_credentials", "payment_required", "model_unavailable", "empty_response", "unsupported_capability",
    "malformed_response", "truncated",
})


class VisualPerceptionError(CognitionError):
    """An expected operational failure, with no provider body or prompt attached."""

    def __init__(self, category: str, *, http_status: int | None = None,
                 failures: Sequence[str] = ()) -> None:
        categories = FALLBACK_CATEGORIES | {
            "refused", "invalid_frame", "invalid_focus", "invalid_request",
            "authentication", "provider_error", "implementation_error", "all_backends_failed",
        }
        category = category if category in categories else "provider_error"
        detail = ",".join(item if item in categories else "provider_error"
                          for item in tuple(failures)[:MAX_VISION_BACKENDS])
        super().__init__(f"Visual perception failed ({category}" +
                         (f"; attempts: {detail}" if detail else "") + ")", category=category)
        self.status_code = http_status


@dataclass(frozen=True, slots=True)
class ImageSnapshot:
    """Historical encoded image; unknown capture time is explicitly None."""

    data: bytes = field(repr=False)
    media_type: str
    width: int
    height: int
    captured_at: datetime | None
    reference: str | None = None
    label: str | None = None
    source: str | None = None
    received_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class VisualPerceptionResult:
    """One bounded, non-authoritative model interpretation."""

    focus: str
    description: str
    truncated: bool = False
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if (self.observed_at is not None and
                (self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None)):
            raise ValueError("visual observed_at must be an offset-aware datetime")


class VisualPerceptionBackend(ABC):
    identifier: str
    request_timeout_seconds: float = 30.0
    supports_multi_image = False

    @abstractmethod
    async def interpret(self, frame: CameraFrame | ImageSnapshot | ImageCollection, focus: str) -> VisualPerceptionResult: ...


@dataclass(frozen=True)
class _Attempt:
    order: int
    timeout: asyncio.Timeout


# Request-local budget and accounting; never mutate a shared SDK client's timeout.
# The existing interpret(frame, focus) interface stays usable by local backends.
_ATTEMPT: ContextVar[_Attempt | None] = ContextVar("visual_attempt", default=None)


def _field(owner: Any, name: str, default: Any = None) -> Any:
    return owner.get(name, default) if isinstance(owner, Mapping) else getattr(owner, name, default)


def _validate_input(frame: CameraFrame | ImageSnapshot | ImageCollection, focus: str) -> None:
    if isinstance(frame, ImageCollection):
        try:
            ImageLimits(max_images=16, max_total_bytes=64 * 1024 * 1024,
                        max_total_pixels=64 * 1024 * 1024).validate(frame.images)
            for image in frame.images:
                _validate_input(ImageSnapshot(image.data, image.media_type, image.width,
                                              image.height, image.captured_at), focus)
        except ValueError:
            raise VisualPerceptionError("invalid_frame") from None
        return
    if (not isinstance(frame, (CameraFrame, ImageSnapshot)) or type(frame.data) is not bytes
            or not 0 < len(frame.data) <= MAX_CAMERA_FRAME_BYTES
            or frame.media_type not in ("image/jpeg", "image/png", "image/webp")
            or any(type(value) is not int or value <= 0 for value in (frame.width, frame.height))
            or (isinstance(frame, CameraFrame) and
                (type(frame.captured_at_ns) is not int or frame.captured_at_ns < 0))
            or (isinstance(frame, ImageSnapshot) and frame.captured_at is not None and
                (not isinstance(frame.captured_at, datetime) or frame.captured_at.utcoffset() is None))):
        raise VisualPerceptionError("invalid_frame")
    if (not isinstance(focus, str) or not focus.strip() or len(focus) > 300
            or any(unicodedata.category(char) == "Cc" for char in focus)):
        raise VisualPerceptionError("invalid_focus")


def _bounded_result(focus: str, description: str) -> VisualPerceptionResult:
    description = description.strip()
    if not description:
        raise VisualPerceptionError("empty_response")
    return VisualPerceptionResult(focus, description[:MAX_VISUAL_DESCRIPTION_CHARS],
                                  len(description) > MAX_VISUAL_DESCRIPTION_CHARS)


def _expected_transport_error(error: Exception) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError, JSONDecodeError)):
        return True
    try:
        from openai import APIError
    except ImportError:
        return False
    return isinstance(error, APIError)


def _usage(response: Any, protocol: str) -> dict[str, int | None] | None:
    usage = _field(response, "usage")
    if usage is None:
        return None
    def token(owner, name):
        value = _field(owner, name)
        return value if type(value) is int and value >= 0 else None
    compatible = protocol == "chat-completions"
    counts = {name: token(usage, source) for name, source in (
        ("input_tokens", "prompt_tokens" if compatible else "input_tokens"),
        ("output_tokens", "completion_tokens" if compatible else "output_tokens"),
        ("total_tokens", "total_tokens"),
    )}
    details = _field(usage, "prompt_tokens_details" if compatible else "input_tokens_details")
    counts["cached_input_tokens"] = token(details, "cached_tokens")
    counts["cache_write_tokens"] = token(details, "cache_write_tokens")
    counts["reasoning_tokens"] = token(_field(usage, "completion_tokens_details" if compatible
                                              else "output_tokens_details"), "reasoning_tokens")
    return counts


class _RemoteVisualBackend(VisualPerceptionBackend):
    """Isolated client and attempt accounting shared by the two visual adapters."""

    protocol: str
    supports_multi_image = True
    image_limits = ImageLimits()

    def __init__(self, config: VisionBackendConfig, *, client: Any = None,
                 observability: RunObservability | None = None) -> None:
        validate_vision_backend_config(config)
        self.config = config
        self.model = config.model
        self.base_url = config.base_url or "https://api.openai.com/v1"
        self.endpoint = endpoint_identity(self.base_url)
        self.request_timeout_seconds = config.request_timeout_seconds
        self._client = client
        self.observability = observability

    def _get_client(self):
        if self._client is None:
            key = os.environ.get(self.config.api_key_env)
            if not key or not key.strip():
                raise VisualPerceptionError("missing_credentials")
            try:
                from openai import AsyncOpenAI
            except ImportError:
                raise VisualPerceptionError("unavailable") from None
            try:
                self._client = AsyncOpenAI(api_key=key, base_url=self.base_url,
                                           timeout=self.request_timeout_seconds, max_retries=0)
            except Exception as error:
                if not _expected_transport_error(error):
                    raise
                raise VisualPerceptionError(failure_category(error), http_status=provider_http_status(error)) from None
        return self._client

    async def interpret(self, frame: CameraFrame | ImageSnapshot | ImageCollection, focus: str) -> VisualPerceptionResult:
        _validate_input(frame, focus)
        images = frame.images if isinstance(frame, ImageCollection) else (frame,)
        self.image_limits.validate(images)
        urls = [f"data:{image.media_type};base64,{base64.b64encode(image.data).decode('ascii')}"
                for image in images]
        arguments = self._arguments(urls[0], focus)
        if isinstance(frame, ImageCollection):
            content = [{"type": "input_text" if self.protocol == "responses" else "text", "text": focus}]
            for image, url in zip(images, urls):
                content.append({"type": "input_text" if self.protocol == "responses" else "text",
                                "text": image.description()})
                content.append({"type": "input_image", "image_url": url, "detail": "auto"} if self.protocol == "responses"
                               else {"type": "image_url", "image_url": {"url": url}})
            if self.protocol == "responses":
                arguments["input"][0]["content"] = content
                arguments["instructions"] = JOINT_IMAGE_INSTRUCTIONS
            else:
                arguments["messages"][1]["content"] = content
                arguments["messages"][0]["content"] = JOINT_IMAGE_INSTRUCTIONS
        LOGGER.info("[VISION] image_count=%s source_ids=%s", len(images),
                    [image.reference for image in images] if isinstance(frame, ImageCollection) else [getattr(frame, "reference", None)])
        if isinstance(frame, ImageSnapshot):
            instructions = (
                "Analyze exactly this one historical saved image. It is not a current "
                "camera observation. Capture time: " +
                (frame.captured_at.isoformat() if frame.captured_at else "unknown") +
                ". Answer the supplied visual focus using visible evidence only. "
                "State uncertainty; do not infer hidden objects, events, or current-world state. Be concise."
            )
            if frame.reference:
                import json
                instructions += " Image metadata: " + json.dumps({"image_ref": frame.reference,
                    "label": frame.label, "source": frame.source,
                    "received_at": frame.received_at.isoformat() if frame.received_at else None})
            if self.protocol == "responses":
                arguments["instructions"] = instructions
            else:
                arguments["messages"][0]["content"] = instructions
        client = self._get_client()  # Setup failures are not outbound calls.
        create = client.responses.create if self.protocol == "responses" else client.chat.completions.create
        attempt = _ATTEMPT.get()
        order = attempt.order if attempt else 1
        budget = self.request_timeout_seconds
        if attempt is not None:
            budget = min(budget, attempt.timeout.when() - asyncio.get_running_loop().time())
        if budget <= 0:
            raise VisualPerceptionError("timeout")
        arguments["timeout"] = budget
        started = time.perf_counter()
        response = None
        category = None
        http_status = None
        status = "completed"
        try:
            scope = asyncio.timeout(budget)
            async with scope:
                response = await create(**arguments)
            result = self._decode(response, focus)
            if asyncio.get_running_loop().time() >= scope.when():
                raise VisualPerceptionError("timeout")
            return result
        except asyncio.CancelledError:
            # A wrapper deadline cancels its child. Preserve external cancellation.
            category = "timeout" if attempt and attempt.timeout.expired() else "cancelled"
            status = "failed" if category == "timeout" else "cancelled"
            raise
        except VisualPerceptionError as error:
            category, http_status, status = error.category, error.status_code, "failed"
            raise
        except Exception as error:
            category = failure_category(error) if _expected_transport_error(error) else "implementation_error"
            http_status, status = provider_http_status(error), "failed"
            if category == "implementation_error":
                raise
            raise VisualPerceptionError(category, http_status=http_status) from None
        finally:
            duration = int((time.perf_counter() - started) * 1000)
            counts = _usage(response, self.protocol)
            returned = _field(response, "model")
            returned = returned if isinstance(returned, str) and returned and not any(
                char.isspace() or ord(char) < 32 for char in returned) else None
            available = counts is not None and all(counts[name] is not None for name in
                                                   ("input_tokens", "output_tokens", "total_tokens"))
            LOGGER.info("[VISION] backend=%s protocol=%s endpoint=%s model=%s returned_model=%s "
                        "attempt_order=%s fallback=%s status=%s duration_ms=%s usage_available=%s "
                        "failure_category=%s http_status=%s", self.identifier, self.protocol,
                        self.endpoint, self.model, returned, order, order > 1, status, duration,
                        available, category, http_status)
            if self.observability is not None:
                self.observability.provider_call(
                    self.identifier, self.model, "interpret", role="vision", status=status,
                    duration_ms=duration, usage=counts, protocol=self.protocol, endpoint=self.endpoint,
                    returned_model=returned, failure_category=category, http_status=http_status,
                    attempt_order=order, fallback=order > 1, accepted=status == "completed",
                )


class OpenAIResponsesVisualPerceptionBackend(_RemoteVisualBackend):
    identifier = "openai-responses"
    protocol = "responses"

    def __init__(self, *, model: str | None = None, api_key_env: str = "OPENAI_API_KEY",
                 request_timeout_seconds: float = 30.0, client: Any = None,
                 observability: RunObservability | None = None) -> None:
        model = model or os.environ.get("OPENAI_VISION_MODEL") or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
        super().__init__(VisionBackendConfig(self.identifier, model, api_key_env,
                                             request_timeout_seconds=request_timeout_seconds),
                         client=client, observability=observability)

    def _arguments(self, image_url, focus):
        return {"model": self.model, "instructions": VISUAL_PROVIDER_INSTRUCTIONS,
                "stream": False, "max_output_tokens": MAX_VISUAL_OUTPUT_TOKENS,
                "input": [{"role": "user", "content": [
                    {"type": "input_text", "text": focus},
                    {"type": "input_image", "image_url": image_url, "detail": "auto"},
                ]}]}

    @staticmethod
    def _decode(response, focus):
        output = _field(response, "output")
        if isinstance(output, list):
            for item in output:
                content = _field(item, "content", [])
                if isinstance(content, list) and any(_field(part, "type") == "refusal" for part in content):
                    raise VisualPerceptionError("refused")
        status = _field(response, "status")
        if status == "incomplete":
            reason = _field(_field(response, "incomplete_details"), "reason")
            raise VisualPerceptionError("refused" if reason == "content_filter" else "truncated")
        if status == "failed":
            code = _field(_field(response, "error"), "code")
            if code is not None and not isinstance(code, str):
                raise VisualPerceptionError("malformed_response")
            raise VisualPerceptionError({"server_error": "provider_unavailable",
                "rate_limit_exceeded": "rate_limit", "invalid_prompt": "invalid_request",
                "content_policy_violation": "refused"}.get(code, "provider_error"))
        if status != "completed" or not isinstance(output, list):
            raise VisualPerceptionError("malformed_response")
        texts = []
        for item in output:
            if _field(item, "type") == "reasoning":
                continue
            content = _field(item, "content")
            if (_field(item, "type") != "message" or _field(item, "role") != "assistant"
                    or not isinstance(content, list)):
                raise VisualPerceptionError("malformed_response")
            for part in content:
                text = _field(part, "text")
                if _field(part, "type") != "output_text" or not isinstance(text, str):
                    raise VisualPerceptionError("malformed_response")
                texts.append(text)
        return _bounded_result(focus, "\n".join(texts))


class OpenAICompatibleVisualPerceptionBackend(_RemoteVisualBackend):
    identifier = "openai-compatible"
    protocol = "chat-completions"

    def __init__(self, *, config: VisionBackendConfig, client: Any = None,
                 observability: RunObservability | None = None) -> None:
        if config.backend != self.identifier:
            raise ConfigurationError("compatible vision requires an openai-compatible entry")
        super().__init__(config, client=client, observability=observability)

    def _arguments(self, image_url, focus):
        arguments = {"model": self.model, "stream": False, "max_tokens": MAX_VISUAL_OUTPUT_TOKENS,
                     "messages": [{"role": "system", "content": VISUAL_PROVIDER_INSTRUCTIONS},
                                  {"role": "user", "content": [
                                      {"type": "text", "text": focus},
                                      {"type": "image_url", "image_url": {"url": image_url}},
                                  ]}]}
        if urlsplit(self.base_url).hostname == "openrouter.ai":
            arguments["extra_body"] = {"provider": {"require_parameters": True, "allow_fallbacks": False}}
        return arguments

    @staticmethod
    def _decode(response, focus):
        choices = _field(response, "choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise VisualPerceptionError("malformed_response")
        choice = choices[0]
        message = _field(choice, "message")
        finish = _field(choice, "finish_reason")
        if finish == "content_filter" or _field(message, "refusal"):
            raise VisualPerceptionError("refused")
        if finish == "length":
            raise VisualPerceptionError("truncated")
        if (finish != "stop" or _field(message, "role") != "assistant"
                or _field(message, "tool_calls") not in (None, [])
                or _field(message, "function_call") is not None):
            raise VisualPerceptionError("malformed_response")
        text = _field(message, "content")
        if not isinstance(text, str):
            raise VisualPerceptionError("empty_response" if text is None else "malformed_response")
        return _bounded_result(focus, text)


class OrderedVisualPerceptionBackend(VisualPerceptionBackend):
    """One frame, a finite sequential chain, and a single total wall deadline."""

    identifier = "ordered-vision"
    supports_multi_image = True
    image_limits = ImageLimits()

    def __init__(self, backends: Sequence[VisualPerceptionBackend], *, request_timeout_seconds: float = 30.0,
                 observability: RunObservability | None = None) -> None:
        validate_vision_configuration(VisionConfiguration(request_timeout_seconds))
        if not 1 <= len(backends) <= MAX_VISION_BACKENDS:
            raise ConfigurationError(f"ordered vision requires 1 to {MAX_VISION_BACKENDS} backends")
        if not all(isinstance(backend, VisualPerceptionBackend) for backend in backends):
            raise ConfigurationError("ordered vision entries must be visual backends")
        for backend in backends:
            validate_vision_configuration(VisionConfiguration(backend.request_timeout_seconds))
        self.backends = tuple(backends)
        self.request_timeout_seconds = request_timeout_seconds
        self.observability = observability

    @property
    def observability(self):
        return self._observability

    @observability.setter
    def observability(self, value):
        self._observability = value
        for backend in self.backends:
            if hasattr(backend, "observability"):
                backend.observability = value

    def _event(self, backend, order, status, category=None):
        if self.observability is not None:
            self.observability.event("perception", "vision_attempt", status,
                source=backend.identifier, error=category, metadata={
                    "attempt_order": order, "fallback": order > 1,
                    "protocol": getattr(backend, "protocol", None),
                    "endpoint": getattr(backend, "endpoint", None),
                    "requested_model": getattr(backend, "model", None),
                })

    async def interpret(self, frame: CameraFrame | ImageSnapshot | ImageCollection, focus: str) -> VisualPerceptionResult:
        _validate_input(frame, focus)
        self.image_limits.validate(frame.images if isinstance(frame, ImageCollection) else (frame,))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.request_timeout_seconds
        failures = []
        for order, backend in enumerate(self.backends, 1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            budget = min(remaining, backend.request_timeout_seconds)
            if order > 1 and self.observability is not None:
                self.observability.increment("vision_fallbacks")
            self._event(backend, order, "started")
            scope = asyncio.timeout(budget)
            token = _ATTEMPT.set(_Attempt(order, scope))
            try:
                async with scope:
                    if (isinstance(frame, ImageCollection) and len(frame.images) > 1
                            and not backend.supports_multi_image):
                        raise VisualPerceptionError("unsupported_capability")
                    result = await backend.interpret(frame, focus)
                if loop.time() >= scope.when():
                    raise VisualPerceptionError("timeout")
                if not isinstance(result, VisualPerceptionResult) or not isinstance(result.description, str):
                    raise TypeError("visual backend returned an invalid result")
                if not result.description.strip():
                    raise VisualPerceptionError("empty_response")
            except TimeoutError:
                if not scope.expired():
                    self._event(backend, order, "failed", "implementation_error")
                    raise
                error = VisualPerceptionError("timeout")
            except VisualPerceptionError as caught:
                error = caught
            except asyncio.CancelledError:
                self._event(backend, order, "cancelled", "cancelled")
                raise
            except Exception:
                self._event(backend, order, "failed", "implementation_error")
                raise
            else:
                self._event(backend, order, "accepted")
                return result
            finally:
                _ATTEMPT.reset(token)
            self._event(backend, order, "failed", error.category)
            if error.category not in FALLBACK_CATEGORIES:
                raise error
            failures.append(error.category)
        raise VisualPerceptionError("all_backends_failed", failures=failures)
