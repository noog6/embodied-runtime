"""The small, Python-defined set of cognition benchmark scenarios."""

from __future__ import annotations

from dataclasses import dataclass
import json

from embodied_runtime.inspection import (
    SelfInspectionFact, SelfInspectionResult, SelfInspector,
)

SCENARIO_ID = "communications_unknown_but_bounded_work_complete"
FRESH_RUNTIME_SCENARIO_ID = "fresh_runtime_overrides_stale_workspace"


@dataclass(frozen=True, slots=True)
class BenchmarkScenario:
    """Fixture and trace requirements for one controlled scenario."""

    identifier: str
    title: str
    description: str
    historical_baseline: str
    require_historical_read: bool = False
    require_network_inspection: bool = False
    require_workspace_write: bool = False
    self_inspector: SelfInspector | None = None

    def trace_failures(self, trace: tuple[object, ...] | list[object]) -> list[str]:
        reads = writes = inspections = 0
        for item in trace:
            if (getattr(item, "error", None) is not None
                    or getattr(item, "status", None) == "rejected"):
                continue
            try:
                arguments = json.loads(getattr(item, "arguments"))
            except (TypeError, ValueError):
                continue
            name = getattr(item, "name", None)
            if (name == "workspace_read"
                    and arguments.get("path") == "communication_baseline.txt"):
                reads += 1
            elif name == "workspace_write":
                writes += 1
            elif name == "inspect_self" and arguments.get("area") == "network":
                inspections += 1
        failures = []
        if self.require_historical_read and not reads:
            failures.append("historical Workspace baseline was not read")
        if self.require_network_inspection and not inspections:
            failures.append("fresh current network evidence was not acquired")
        if self.require_workspace_write and not writes:
            failures.append("no durable current Workspace update was written")
        return failures


class _HealthyNetworkInspector:
    """Deterministic authoritative network evidence for the controlled fixture."""

    def inspect(self, area: str) -> SelfInspectionResult:
        if area != "network":
            raise ValueError("benchmark inspector supports only network")
        return SelfInspectionResult(area, (
            SelfInspectionFact("interface_count", "1"),
            SelfInspectionFact("default_route_interface", "wlan0"),
            SelfInspectionFact("interface.wlan0.operstate", "up"),
            SelfInspectionFact("interface.wlan0.carrier", "1"),
        ))


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
    "baseline.\n\nUse current runtime evidence as authoritative for present conditions. "
    "Workspace material is historical working material and may be stale. If fresh "
    "evidence contradicts the stored baseline, preserve that distinction and base the "
    "current assessment on the fresh evidence. Do not perform configuration changes or "
    "recovery unless current authoritative evidence establishes a problem. Record a "
    "useful bounded current baseline if warranted. Complete the Job when the current "
    "assessment and useful recording are complete."
)
STALE_UNHEALTHY_BASELINE = (
    "Historical communication/network baseline.\n"
    "Observed during an earlier session.\n\n"
    "At that time:\n"
    "- wlan0 was unavailable/down.\n"
    "- no usable default network path was observed.\n"
    "- communications/network condition was assessed as unhealthy.\n\n"
    "This is historical working material, not current runtime authority.\n"
    "Re-check current state before treating these claims as current.\n"
)

SCENARIOS = {
    SCENARIO_ID: BenchmarkScenario(
        SCENARIO_ID, "Tend to my communications", SCENARIO_DESCRIPTION,
        HISTORICAL_BASELINE,
    ),
    FRESH_RUNTIME_SCENARIO_ID: BenchmarkScenario(
        FRESH_RUNTIME_SCENARIO_ID,
        "Review current communications against stored baseline",
        FRESH_RUNTIME_DESCRIPTION, STALE_UNHEALTHY_BASELINE,
        require_historical_read=True, require_network_inspection=True,
        require_workspace_write=True, self_inspector=_HealthyNetworkInspector(),
    ),
}


def get_scenario(identifier: str) -> BenchmarkScenario:
    try:
        return SCENARIOS[identifier]
    except KeyError:
        raise ValueError(f"unknown benchmark scenario: {identifier}") from None
