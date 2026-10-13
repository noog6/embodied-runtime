"""Shared runtime/benchmark construction; no provider calls during validation."""

from dataclasses import replace
from typing import Any

from embodied_runtime.config import (
    ConfigurationError, OpenAICompatibleConfig, validate_compatible_config,
)
from embodied_runtime.cognition.base import TextCognitionBackend
from embodied_runtime.cognition.openai_responses import OpenAIResponsesBackend


def validate_cognition_configuration(
    backend: str, model: str | None, compatible: OpenAICompatibleConfig,
) -> None:
    if backend not in {"none", "openai-responses", "openai-compatible"}:
        raise ConfigurationError("unknown cognition backend")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ConfigurationError("cognition model must be a non-empty string")
    if backend == "openai-compatible":
        validate_compatible_config(replace(
            compatible, model=model if model is not None else compatible.model,
        ))


def create_cognition_backend(
    backend: str, *, model: str | None = None,
    compatible: OpenAICompatibleConfig = OpenAICompatibleConfig(),
    client: Any = None,
) -> TextCognitionBackend | None:
    validate_cognition_configuration(backend, model, compatible)
    if backend == "openai-responses":
        return OpenAIResponsesBackend(model=model, client=client)
    if backend == "openai-compatible":
        from embodied_runtime.cognition.openai_compatible import OpenAICompatibleBackend
        return OpenAICompatibleBackend(
            config=replace(compatible, model=model if model is not None else compatible.model),
            client=client,
        )
    return None


def add_compatible_arguments(parser: Any) -> None:
    """Identical dedicated connection/capability flags in both entry points."""
    import argparse

    parser.add_argument("--compatible-base-url")
    parser.add_argument("--compatible-api-key-env", help="credential variable name, never its value")
    parser.add_argument("--compatible-request-timeout-seconds", type=float)
    parser.add_argument("--compatible-tool-calling", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--compatible-image-input", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument(
        "--compatible-parallel-tool-calls-mode", choices=("disable", "omit"),
        help="send parallel_tool_calls=False (disable, default) or omit the parameter; runtime still rejects multiple calls",
    )
