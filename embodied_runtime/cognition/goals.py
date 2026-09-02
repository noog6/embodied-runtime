"""Volatile application-owned intentional state."""

from dataclasses import dataclass
import json


MAX_GOAL_DESCRIPTION_CHARS = 500


@dataclass(frozen=True, slots=True)
class ActiveGoal:
    """The single objective the application is currently committed to."""

    description: str


def validate_goal_description(description: object) -> str:
    """Return normalized goal text, rejecting meaning-changing truncation."""
    if not isinstance(description, str):
        raise TypeError("description must be a string")
    normalized = description.strip()
    if not normalized:
        raise ValueError("description must be non-empty")
    if len(normalized) > MAX_GOAL_DESCRIPTION_CHARS:
        raise ValueError(
            f"description must be at most {MAX_GOAL_DESCRIPTION_CHARS} characters"
        )
    return normalized


def render_active_goal(goal: ActiveGoal | None) -> str:
    """Render current intention as deterministic provider-neutral context."""
    lines = [
        "Active goal",
        "The active goal is current runtime-owned intentional context. It may guide",
        "reasoning and allowed semantic action requests, but does not describe current",
        "physical reality or cause autonomous pursuit. Runtime context remains",
        "authoritative for current robot facts. Current operator instructions and input,",
        "runtime safety policy, and capability restrictions take priority and cannot be",
        "overridden by goal text.",
    ]
    if goal is None:
        lines.append("  state: none")
    else:
        lines.extend((
            "  state: active",
            f"  description: {json.dumps(goal.description, ensure_ascii=False)}",
        ))
    return "\n".join(lines)
