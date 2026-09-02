"""Minimal provider-neutral text cognition and tool-call contract."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class CognitionError(RuntimeError):
    """A text cognition request could not be completed."""


class CognitionUnavailableError(CognitionError):
    """The configured cognition backend is unavailable."""


@dataclass(frozen=True, slots=True)
class CognitionToolDefinition:
    """One explicitly offered function and its JSON Schema arguments."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CognitionToolCall:
    """One provider-requested invocation with untrusted JSON arguments."""

    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class CognitionToolResult:
    """Runtime-produced JSON text returned to the provider."""

    output: str


CognitionToolExecutor = Callable[[CognitionToolCall], Awaitable[CognitionToolResult]]
InstructionsProvider = Callable[[], str]


class TextCognitionBackend(ABC):
    """One independent operator or runtime-generated text request and response."""

    identifier: str

    @abstractmethod
    async def respond(
        self,
        message: str,
        *,
        instructions: str | None = None,
        tools: Sequence[CognitionToolDefinition] = (),
        tool_executor: CognitionToolExecutor | None = None,
        refreshed_instructions: InstructionsProvider | None = None,
    ) -> str:
        """Return the text response to one independent request."""
