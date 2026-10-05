"""Immutable benchmark report structures and JSON conversion."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json

from .recording import CognitionRequestRecord, ToolTraceEntry

FORMAT_VERSION = 2


@dataclass(frozen=True, slots=True)
class TrialMetrics:
    provider_requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    job_work_episodes: int = 0
    continuation_count: int = 0
    acquisition_tool_calls: int = 0
    effect_tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class BenchmarkTrialResult:
    scenario_id: str
    backend: str
    model: str
    repetition: int
    passed: bool
    failure_reasons: tuple[str, ...]
    error: str | None
    wall_duration_seconds: float
    estimated_cost_usd: str | None
    pricing_identity: str
    final_job_run_status: str | None
    final_disposition: str | None
    final_continuation_state: str | None
    final_response: str
    metrics: TrialMetrics
    requests: tuple[CognitionRequestRecord, ...]
    tool_trace: tuple[ToolTraceEntry, ...]

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["failure_reasons"] = list(self.failure_reasons)
        return value


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    created_at: str
    scenario_id: str
    trials: tuple[BenchmarkTrialResult, ...]
    pricing_identity: str
    format_version: int = FORMAT_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "format_version": self.format_version,
            "created_at": self.created_at,
            "scenario_id": self.scenario_id,
            "pricing_identity": self.pricing_identity,
            "trials": [trial.as_dict() for trial in self.trials],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"
