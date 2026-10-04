"""Provider-neutral, bounded recording around production text cognition."""

from __future__ import annotations

from dataclasses import dataclass
import json
from time import perf_counter
from typing import Any
from collections.abc import Sequence

from embodied_runtime.attachments import ImageAttachment
from embodied_runtime.cognition.base import (
    CognitionToolCall, CognitionToolDefinition, CognitionToolExecutor,
    CognitionToolResult, InstructionsProvider, TextCognitionBackend,
)

MAX_CAPTURE_CHARS = 4_000


def _bounded(value: str) -> str:
    return value[:MAX_CAPTURE_CHARS]


@dataclass(frozen=True, slots=True)
class ToolTraceEntry:
    ordinal: int
    request_ordinal: int
    name: str
    arguments: str
    result: str | None
    status: str | None
    error: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal, "request_ordinal": self.request_ordinal,
            "name": self.name, "arguments": self.arguments,
            "result": self.result, "status": self.status, "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class CognitionRequestRecord:
    ordinal: int
    kind: str
    offered_tools: tuple[str, ...]
    duration_ms: int
    response_text: str
    error: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal, "kind": self.kind,
            "offered_tools": list(self.offered_tools),
            "duration_ms": self.duration_ms,
            "response_text": self.response_text, "error": self.error,
        }


class RecordingCognitionBackend(TextCognitionBackend):
    """Delegate every operation and observe calls through the supplied executor."""

    def __init__(self, delegate: TextCognitionBackend) -> None:
        self.delegate = delegate
        self.identifier = f"recording:{delegate.identifier}"
        self.supports_image_input = delegate.supports_image_input
        self.requests: list[CognitionRequestRecord] = []
        self.tool_trace: list[ToolTraceEntry] = []

    @property
    def model(self) -> str | None:
        value = getattr(self.delegate, "model", None)
        return value if isinstance(value, str) else None

    @property
    def observability(self) -> object | None:
        return getattr(self.delegate, "observability", None)

    @observability.setter
    def observability(self, value: object) -> None:
        if hasattr(self.delegate, "observability"):
            self.delegate.observability = value

    async def prepare(self) -> None:
        await self.delegate.prepare()

    @staticmethod
    def _kind(
        message: str, instructions: str | None,
        tools: Sequence[CognitionToolDefinition],
    ) -> str:
        if any(tool.name == "report_job_outcome" for tool in tools):
            return "job_outcome"
        if "kind: job_run_work" in (instructions or ""):
            return "job_work"
        return "cognition"

    async def respond(
        self, message: str, *, instructions: str | None = None,
        tools: Sequence[CognitionToolDefinition] = (),
        tool_executor: CognitionToolExecutor | None = None,
        refreshed_instructions: InstructionsProvider | None = None,
        image_attachments: Sequence[ImageAttachment] = (),
    ) -> str:
        ordinal = len(self.requests) + 1
        started = perf_counter()

        async def recording_executor(call: CognitionToolCall) -> CognitionToolResult:
            if tool_executor is None:
                raise RuntimeError("delegate requested a tool without a runtime executor")
            try:
                result = await tool_executor(call)
            except BaseException as caught:
                detail = f"{type(caught).__name__}: {caught}"
                self.tool_trace.append(ToolTraceEntry(
                    len(self.tool_trace) + 1, ordinal, call.name,
                    _bounded(call.arguments), None, None, _bounded(detail),
                ))
                raise
            status = None
            try:
                decoded: Any = json.loads(result.output)
                if isinstance(decoded, dict) and isinstance(decoded.get("status"), str):
                    status = decoded["status"]
            except (json.JSONDecodeError, TypeError):
                pass
            self.tool_trace.append(ToolTraceEntry(
                len(self.tool_trace) + 1, ordinal, call.name,
                _bounded(call.arguments), _bounded(result.output), status, None,
            ))
            return result

        error = None
        response = ""
        try:
            response = await self.delegate.respond(
                message, instructions=instructions, tools=tools,
                tool_executor=(recording_executor if tool_executor is not None else None),
                refreshed_instructions=refreshed_instructions,
                image_attachments=image_attachments,
            )
            return response
        except BaseException as caught:
            error = type(caught).__name__
            raise
        finally:
            self.requests.append(CognitionRequestRecord(
                ordinal, self._kind(message, instructions, tools),
                tuple(tool.name for tool in tools),
                int((perf_counter() - started) * 1_000), _bounded(response), error,
            ))
