"""Provider-neutral grounding for a bounded autonomous effect sequence."""

from dataclasses import dataclass
import json


@dataclass(frozen=True, slots=True)
class InitiativeEffectOutcome:
    """One immutable runtime-produced semantic effect result."""

    name: str
    status: str
    runtime_result: str


@dataclass(frozen=True, slots=True)
class InitiativeAcquisitionOutcome:
    """One ordered, request-local read-only acquisition attempt."""

    capability: str
    status: str
    runtime_result: str
    inspection_result: object | None = None
    perception_result: object | None = None

    def render(self, index: int) -> list[str]:
        lines = [
            f"  acquisition_{index}_capability: {self.capability}",
            f"  acquisition_{index}_status: {self.status}",
        ]
        if self.inspection_result is not None:
            result = self.inspection_result
            lines.append(
                f"  acquisition_{index}_authority: runtime-produced authoritative "
                "facts for the inspected area"
            )
            lines.append(f"  acquisition_{index}_area: {result.area}")
            lines.extend(
                f"  acquisition_{index}.{fact.name}: {fact.value}"
                for fact in result.facts
            )
        elif self.perception_result is not None:
            result = self.perception_result
            lines.extend((
                f"  acquisition_{index}_authority: model-generated visual interpretation; "
                "may be incomplete or uncertain; not authoritative Runtime state",
                f"  acquisition_{index}_visual_focus: {result.focus}",
                f"  acquisition_{index}_visual_description: {result.description}",
                f"  acquisition_{index}_visual_description_truncated: "
                f"{str(result.truncated).lower()}",
            ))
        else:
            lines.append(
                f"  acquisition_{index}_result: "
                f"{json.dumps(self.runtime_result, ensure_ascii=False)}"
            )
        return lines


@dataclass(frozen=True, slots=True)
class GoalOutcomeStimulus:
    """Immutable semantic facts produced by one or two initiative attempts."""

    effects: tuple[InitiativeEffectOutcome, ...]
    attention_kind: str
    attention_source: str
    acquisitions: tuple[InitiativeAcquisitionOutcome, ...] = ()

    def __post_init__(self) -> None:
        if len(self.effects) not in (1, 2):
            raise ValueError("outcome stimulus requires one or two effects")
        if len(self.acquisitions) > 2:
            raise ValueError("outcome stimulus permits at most two acquisitions")

    def render(self) -> str:
        lines = [
            "Goal outcome stimulus",
            "These are the runtime-produced results of the bounded autonomous effects.",
            *(
                line
                for index, effect in enumerate(self.effects, start=1)
                for line in (
                    f"  effect_{index}_name: {effect.name}",
                    f"  effect_{index}_status: {effect.status}",
                    f"  effect_{index}_result: {json.dumps(effect.runtime_result, ensure_ascii=False)}",
                )
            ),
            f"  attention_kind: {self.attention_kind}",
            f"  attention_source: {self.attention_source}",
        ]
        if self.acquisitions:
            lines.append("Prior ordered acquisition evidence (request-local, not effects):")
            for index, acquisition in enumerate(self.acquisitions, start=1):
                lines.extend(acquisition.render(index))
        return "\n".join(lines)
