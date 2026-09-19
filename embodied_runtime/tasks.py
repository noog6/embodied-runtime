"""Immutable domain model for bounded units of meaningful work."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid4


MAX_TASK_DESCRIPTION_CHARS = 500


class TaskStatus(StrEnum):
    """Lifecycle states for a :class:`Task`."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class InvalidTaskTransitionError(ValueError):
    """Raised when a task cannot move between two lifecycle states."""


_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset((TaskStatus.RUNNING, TaskStatus.STOPPED)),
    TaskStatus.RUNNING: frozenset((
        TaskStatus.PAUSED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.STOPPED,
    )),
    TaskStatus.PAUSED: frozenset((TaskStatus.RUNNING, TaskStatus.STOPPED)),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.STOPPED: frozenset(),
}


@dataclass(frozen=True, slots=True, init=False)
class Task:
    """An immutable snapshot of a bounded unit of meaningful work."""

    id: UUID
    description: str
    status: TaskStatus

    def __init__(self, description: str, *, id: UUID | None = None) -> None:
        """Create a pending task, generating a durable identity when omitted."""
        task_id = uuid4() if id is None else id
        if not isinstance(task_id, UUID):
            raise TypeError("id must be a UUID")
        if task_id.int == 0:
            raise ValueError("id must not be the nil UUID")
        normalized = validate_task_description(description)
        object.__setattr__(self, "id", task_id)
        object.__setattr__(self, "description", normalized)
        object.__setattr__(self, "status", TaskStatus.PENDING)

    def transition_to(self, status: TaskStatus) -> "Task":
        """Return the next lifecycle snapshot, rejecting invalid transitions."""
        if not isinstance(status, TaskStatus):
            raise TypeError("status must be a TaskStatus")
        if status not in _TRANSITIONS[self.status]:
            raise InvalidTaskTransitionError(
                f"task cannot transition from {self.status.value} to {status.value}"
            )

        task = object.__new__(Task)
        object.__setattr__(task, "id", self.id)
        object.__setattr__(task, "description", self.description)
        object.__setattr__(task, "status", status)
        return task


def validate_task_description(description: object) -> str:
    """Return normalized task text, rejecting invalid or oversized values."""
    if not isinstance(description, str):
        raise TypeError("description must be a string")
    normalized = description.strip()
    if not normalized:
        raise ValueError("description must be non-empty")
    if len(normalized) > MAX_TASK_DESCRIPTION_CHARS:
        raise ValueError(
            f"description must be at most {MAX_TASK_DESCRIPTION_CHARS} characters"
        )
    return normalized
