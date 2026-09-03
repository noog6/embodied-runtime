"""Provider-neutral grounding for one autonomous action outcome."""

from dataclasses import dataclass
import json


@dataclass(frozen=True, slots=True)
class GoalOutcomeStimulus:
    """Immutable semantic facts produced by one initiative action attempt."""

    action_name: str
    action_status: str
    action_result: str
    attention_kind: str
    attention_source: str

    def render(self) -> str:
        return "\n".join((
            "Goal outcome stimulus",
            "This is the runtime-produced result of the autonomous action attempt.",
            f"  action: {self.action_name}",
            f"  status: {self.action_status}",
            f"  result: {json.dumps(self.action_result, ensure_ascii=False)}",
            f"  attention_kind: {self.attention_kind}",
            f"  attention_source: {self.attention_source}",
        ))
