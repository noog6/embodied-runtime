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


@dataclass(frozen=True, slots=True)
class BenchmarkScenario:
    identifier: str
    job_title: str
    description: str
    historical_baseline: str
    self_inspector_factory: Callable[[], SelfInspector] | None = None
    require_historical_read: bool = False
    require_network_inspection: bool = False
    require_workspace_write: bool = False

    def evaluate_trace(
        self, trace: tuple[ToolTraceEntry, ...],
        *, historical_content_version: str | None = None,
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
}


def get_scenario(identifier: str) -> BenchmarkScenario:
    try:
        return SCENARIOS[identifier]
    except KeyError:
        raise ValueError(f"unknown benchmark scenario: {identifier}") from None
