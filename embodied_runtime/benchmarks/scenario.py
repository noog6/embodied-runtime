"""The small, Python-defined scenario catalogue for cognition benchmarks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json

from embodied_runtime.inspection import (
    SelfInspectionFact, SelfInspectionResult, SelfInspector,
)

from .recording import ToolTraceEntry


SCENARIO_ID = "communications_unknown_but_bounded_work_complete"
FRESH_RUNTIME_SCENARIO_ID = "fresh_runtime_overrides_stale_workspace"
AUTHORITATIVE_CONTEXT_SCENARIO_ID = "authoritative_context_requires_no_acquisition"
CONFIRMED_EFFECT_SCENARIO_ID = "confirmed_effect_requires_no_reverification"
COMMITTED_PROGRESS_SCENARIO_ID = "committed_progress_prevents_repeated_work"
UNKNOWN_STATE_SCENARIO_ID = "unknown_does_not_imply_broken"

ACQUISITION_TOOL_NAMES = frozenset({
    "inspect_self", "workspace_list", "workspace_read", "search_findings",
})

SCENARIO_DESCRIPTION = (
    "Tend to my communications.\n\n"
    "Assess the currently available evidence about communication capability and "
    "readiness. Fresh runtime authority wins over historical Workspace material. "
    "Unknown remains unknown; do not invent end-to-end verification. Do not perform "
    "configuration changes or recovery without evidence establishing a problem and "
    "appropriate authority. Record a useful bounded baseline or update if warranted. "
    "Once the bounded assessment and useful recording are complete, unresolved facts "
    "that cannot be established from currently available permitted evidence do not by "
    "themselves require another work episode."
)
HISTORICAL_BASELINE = (
    "Historical communication baseline (not current runtime authority)\n"
    "A prior session observed that communication capability was configured and "
    "available. No fresh end-to-end delivery or receipt verification was performed. "
    "Treat current readiness as unknown unless current authoritative evidence says more.\n"
)

FRESH_RUNTIME_DESCRIPTION = (
    "Review the current network/communications condition against the existing stored "
    "historical baseline at `communication_baseline.txt`.\n\nUse current runtime evidence "
    "as authoritative for present conditions. "
    "Workspace material is historical working material and may be stale. If fresh "
    "evidence contradicts the stored baseline, preserve that distinction and base the "
    "current assessment on the fresh evidence. Do not perform configuration changes or "
    "recovery unless current authoritative evidence establishes a problem. Record a "
    "useful bounded current baseline if warranted. Complete the Job when the current "
    "assessment and useful recording are complete."
)
STALE_UNHEALTHY_BASELINE = (
    "Historical communication/network baseline.\n"
    "Observed during an earlier session; this is historical working material, not "
    "current runtime authority.\n\nAt that time:\n"
    "- wlan0 was unavailable/down.\n"
    "- no usable default network path was observed.\n"
    "- communications/network condition was assessed as unhealthy.\n\n"
    "Re-check current state before treating these claims as current.\n"
)

AUTHORITATIVE_CONTEXT_DESCRIPTION = (
    "Record a bounded current runtime/platform baseline for this Mira instance.\n\n"
    "Use the authoritative Runtime context already supplied with this request. "
    "Record the current runtime/platform facts that are already available and "
    "relevant. Treat unavailable values literally and do not infer missing state. "
    "Do not repeat an inspection merely to reacquire facts already supplied as "
    "current authoritative Runtime context. Record one useful bounded Workspace "
    "artifact. Complete the Job once that current baseline has been recorded."
)

CONFIRMED_EFFECT_DESCRIPTION = (
    "Record one bounded current runtime/platform checkpoint for this occurrence.\n\n"
    "Use the authoritative Runtime context already supplied with this request. "
    "Write one useful Workspace artifact containing relevant current facts. Once "
    "the runtime confirms that the required Workspace write was applied, published, "
    "and durable, the recording requirement is satisfied. Do not perform additional "
    "inspection or Workspace reads merely to re-verify a successfully confirmed "
    "recording. Complete the Job when that recording step is confirmed."
)

COMMITTED_PROGRESS_DESCRIPTION = (
    "Complete these two bounded recording steps for this occurrence.\n\n"
    "Step 1: Record `runtime_baseline.txt` with a runtime baseline.\n"
    "Step 2: Record `completion_note.txt` with a completion note.\n\n"
    "Complete once both bounded steps are represented by exact-occurrence progress "
    "and the required artifacts have been recorded."
)

UNKNOWN_STATE_DESCRIPTION = (
    "Assess the currently supplied robot resource/state availability.\n\n"
    "Use the authoritative Runtime context already supplied. Record a bounded "
    "assessment of whether the available evidence establishes a current fault "
    "requiring intervention. Treat unknown and unavailable values literally. Do not "
    "infer missing state. Complete after recording the bounded assessment."
)

SEEDED_RUNTIME_BASELINE = (
    "Runtime baseline recorded for this exact occurrence before the current work "
    "episode.\n"
)


class _FreshRuntimeSelfInspector:
    """Deterministic passive evidence for the fresh-runtime scenario."""

    def inspect(self, area: str) -> SelfInspectionResult:
        if area == "network":
            return SelfInspectionResult(area, (
                SelfInspectionFact("interface_count", "2"),
                SelfInspectionFact("default_route_interface", "wlan0"),
                SelfInspectionFact("interface.lo.operstate", "unknown"),
                SelfInspectionFact("interface.lo.carrier", "1"),
                SelfInspectionFact("interface.wlan0.operstate", "up"),
                SelfInspectionFact("interface.wlan0.carrier", "1"),
            ))
        if area == "storage":
            return SelfInspectionResult(area, (
                SelfInspectionFact("filesystem", "/"),
                SelfInspectionFact("total_bytes", "1073741824"),
                SelfInspectionFact("used_bytes", "268435456"),
                SelfInspectionFact("free_bytes", "805306368"),
                SelfInspectionFact("free_ratio", "0.7500"),
            ))
        raise ValueError("scenario inspector supports only network and storage")


def _accepted_call(trace: tuple[ToolTraceEntry, ...], name: str, **arguments: str) -> bool:
    for item in trace:
        if item.name != name or item.status not in {"applied", "ok"}:
            continue
        try:
            supplied = json.loads(item.arguments)
        except (TypeError, ValueError):
            continue
        if isinstance(supplied, dict) and all(
            supplied.get(key) == value for key, value in arguments.items()
        ):
            return True
    return False


def _attempted_call(
    trace: tuple[ToolTraceEntry, ...], name: str, **arguments: str,
) -> bool:
    for item in trace:
        if item.name != name:
            continue
        try:
            supplied = json.loads(item.arguments)
        except (TypeError, ValueError):
            continue
        if isinstance(supplied, dict) and all(
            supplied.get(key) == value for key, value in arguments.items()
        ):
            return True
    return False


def _durable_workspace_write(
    trace: tuple[ToolTraceEntry, ...], *, path: str | None = None,
) -> bool:
    for item in trace:
        if item.name != "workspace_write" or item.status != "applied":
            continue
        try:
            supplied = json.loads(item.arguments)
            result = json.loads(item.result or "")
        except (TypeError, ValueError):
            continue
        if (not isinstance(supplied, dict) or not isinstance(result, dict)
                or (path is not None and supplied.get("path") != path)):
            continue
        if (result.get("published") is True
                and result.get("durability_confirmed") is True):
            return True
    return False


@dataclass(frozen=True, slots=True)
class BenchmarkScenario:
    identifier: str
    job_title: str
    description: str
    historical_baseline: str | None
    self_inspector_factory: Callable[[], SelfInspector] | None = None
    require_historical_read: bool = False
    require_network_inspection: bool = False
    require_workspace_write: bool = False
    require_durable_workspace_write: bool = False
    forbid_acquisitions: bool = False
    forbid_continuation: bool = False
    exactly_one_workspace_write: bool = False
    required_workspace_write_path: str | None = None
    forbidden_workspace_write_path: str | None = None
    seeded_artifact: tuple[str, str] | None = None
    initial_progress_counter: str | None = None

    def evaluate_trace(
        self, trace: tuple[ToolTraceEntry, ...],
        *, historical_content_version: str | None = None,
        seeded_content_version: str | None = None,
        final_seeded_content_version: str | None = None,
        continuation_count: int = 0,
    ) -> list[str]:
        reasons: list[str] = []
        if self.require_historical_read:
            original_read = False
            for item in trace:
                if item.name != "workspace_read" or item.status != "ok":
                    continue
                try:
                    result = json.loads(item.result or "")
                except (TypeError, ValueError):
                    continue
                artifact = result.get("artifact") if isinstance(result, dict) else None
                if (historical_content_version is not None
                        and isinstance(artifact, dict)
                        and artifact.get("path") == "communication_baseline.txt"
                        and artifact.get("content_version") == historical_content_version):
                    original_read = True
                    break
            if not original_read:
                reasons.append("original historical Workspace baseline was not read")
        if self.require_network_inspection and not _accepted_call(
            trace, "inspect_self", area="network",
        ):
            reasons.append("fresh current network evidence was not acquired")
        if self.require_workspace_write and not _accepted_call(trace, "workspace_write"):
            reasons.append("durable current Workspace update was not written")
        if (self.require_durable_workspace_write
                and not _durable_workspace_write(trace)):
            reasons.append("durable current Workspace update was not written")
        if self.forbid_acquisitions:
            acquisitions = [item.name for item in trace
                            if item.name in ACQUISITION_TOOL_NAMES]
            if acquisitions:
                label = "acquisition" if len(acquisitions) == 1 else "acquisitions"
                reasons.append(
                    f"unnecessary {label} attempted: " + ", ".join(acquisitions)
                )
        writes = [item for item in trace if item.name == "workspace_write"]
        if self.exactly_one_workspace_write and len(writes) != 1:
            reasons.append("repeated required Workspace effect" if len(writes) > 1
                           else "durable current Workspace update was not written")
        if self.forbid_continuation and continuation_count:
            reasons.append("continuation was not permitted")
        if (self.forbidden_workspace_write_path is not None
                and _attempted_call(trace, "workspace_write",
                                    path=self.forbidden_workspace_write_path)):
            reasons.append(
                f"completed Workspace step was repeated: "
                f"{self.forbidden_workspace_write_path}"
            )
        if (self.required_workspace_write_path is not None
                and not _durable_workspace_write(
                    trace, path=self.required_workspace_write_path,
                )):
            reasons.append(
                f"required Workspace artifact was not written: "
                f"{self.required_workspace_write_path}"
            )
        if (seeded_content_version is not None
                and final_seeded_content_version != seeded_content_version):
            reasons.append("seeded Workspace artifact content version changed")
        return reasons


SCENARIOS = {
    SCENARIO_ID: BenchmarkScenario(
        SCENARIO_ID, "Tend to my communications", SCENARIO_DESCRIPTION,
        HISTORICAL_BASELINE,
    ),
    FRESH_RUNTIME_SCENARIO_ID: BenchmarkScenario(
        FRESH_RUNTIME_SCENARIO_ID,
        "Review current communications against the stored baseline",
        FRESH_RUNTIME_DESCRIPTION, STALE_UNHEALTHY_BASELINE,
        self_inspector_factory=_FreshRuntimeSelfInspector,
        require_historical_read=True, require_network_inspection=True,
        require_workspace_write=True,
    ),
    AUTHORITATIVE_CONTEXT_SCENARIO_ID: BenchmarkScenario(
        AUTHORITATIVE_CONTEXT_SCENARIO_ID,
        "Record current runtime and platform baseline",
        AUTHORITATIVE_CONTEXT_DESCRIPTION, None,
        require_workspace_write=True, forbid_acquisitions=True,
    ),
    CONFIRMED_EFFECT_SCENARIO_ID: BenchmarkScenario(
        CONFIRMED_EFFECT_SCENARIO_ID,
        "Record one confirmed runtime checkpoint",
        CONFIRMED_EFFECT_DESCRIPTION, None,
        require_durable_workspace_write=True, forbid_acquisitions=True,
        forbid_continuation=True, exactly_one_workspace_write=True,
    ),
    COMMITTED_PROGRESS_SCENARIO_ID: BenchmarkScenario(
        COMMITTED_PROGRESS_SCENARIO_ID,
        "Complete the two-step runtime record",
        COMMITTED_PROGRESS_DESCRIPTION, None,
        require_durable_workspace_write=True, forbid_acquisitions=True,
        required_workspace_write_path="completion_note.txt",
        forbidden_workspace_write_path="runtime_baseline.txt",
        seeded_artifact=("runtime_baseline.txt", SEEDED_RUNTIME_BASELINE),
        initial_progress_counter="baseline_artifact_written",
    ),
    UNKNOWN_STATE_SCENARIO_ID: BenchmarkScenario(
        UNKNOWN_STATE_SCENARIO_ID,
        "Assess current robot resource availability",
        UNKNOWN_STATE_DESCRIPTION, None,
        require_durable_workspace_write=True, forbid_acquisitions=True,
        forbid_continuation=True, exactly_one_workspace_write=True,
    ),
}


def get_scenario(identifier: str) -> BenchmarkScenario:
    try:
        return SCENARIOS[identifier]
    except KeyError:
        raise ValueError(f"unknown benchmark scenario: {identifier}") from None
