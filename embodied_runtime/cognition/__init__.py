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
from embodied_runtime.cognition.working_memory import (
    WorkingMemory,
    WorkingMemoryToolOutcome,
    WorkingMemoryTurn,
    render_working_memory,
)

__all__ = [
    "CognitionContext",
    "ActiveGoal",
    "MAX_GOAL_DESCRIPTION_CHARS",
    "CognitionError",
    "CognitionToolCall",
    "CognitionToolDefinition",
    "CognitionToolResult",
    "CognitionUnavailableError",
    "TextCognitionBackend",
    "WorkingMemory",
    "WorkingMemoryToolOutcome",
    "WorkingMemoryTurn",
    "compose_cognition_instructions",
    "render_working_memory",
    "render_active_goal",
    "validate_goal_description",
]
