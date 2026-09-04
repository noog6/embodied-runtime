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
class GoalOutcomeStimulus:
    """Immutable semantic facts produced by one or two initiative attempts."""

    effects: tuple[InitiativeEffectOutcome, ...]
    attention_kind: str
    attention_source: str
    inspection_result: object | None = None

    def __post_init__(self) -> None:
        if len(self.effects) not in (1, 2):
            raise ValueError("outcome stimulus requires one or two effects")

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
        if self.inspection_result is not None:
            result = self.inspection_result
            lines.append(f"  prior_self_inspection_area: {result.area}")
            lines.extend(
                f"  prior_self_inspection.{fact.name}: {fact.value}"
                for fact in result.facts
            )
        return "\n".join(lines)
