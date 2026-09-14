"""Optional text cognition boundaries."""

from embodied_runtime.cognition.base import (
    CognitionError,
    CognitionToolCall,
    CognitionToolDefinition,
    CognitionToolResult,
    CognitionUnavailableError,
    TextCognitionBackend,
)
from embodied_runtime.cognition.context import (
    CognitionContext,
    compose_cognition_instructions,
)
from embodied_runtime.cognition.goals import (
    ActiveGoal,
    MAX_GOAL_DESCRIPTION_CHARS,
    render_active_goal,
    validate_goal_description,
)
from embodied_runtime.cognition.outcome import (
    EpisodeAcquisitionOutcome, GoalOutcomeStimulus,
    InitiativeAcquisitionOutcome, InitiativeEffectOutcome,
)
from embodied_runtime.cognition.working_memory import (
    WorkingMemory,
    WorkingMemoryObservation,
    WorkingMemoryToolOutcome,
    WorkingMemoryTurn,
    render_working_memory,
)

__all__ = [
    "CognitionContext",
    "GoalOutcomeStimulus",
    "EpisodeAcquisitionOutcome",
    "InitiativeAcquisitionOutcome",
    "InitiativeEffectOutcome",
    "ActiveGoal",
    "MAX_GOAL_DESCRIPTION_CHARS",
    "CognitionError",
    "CognitionToolCall",
    "CognitionToolDefinition",
    "CognitionToolResult",
    "CognitionUnavailableError",
    "TextCognitionBackend",
    "WorkingMemory",
    "WorkingMemoryObservation",
    "WorkingMemoryToolOutcome",
    "WorkingMemoryTurn",
    "compose_cognition_instructions",
    "render_working_memory",
    "render_active_goal",
    "validate_goal_description",
]
