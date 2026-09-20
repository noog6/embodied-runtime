"""Transient results for explicitly invoked bounded Job work."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class JobWorkDisposition(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CONTINUE = "continue"


@dataclass(frozen=True, slots=True)
class JobWorkOutcome:
    job_id: int
    run_id: int
    task_id: UUID
    episode_id: int
    disposition: JobWorkDisposition
    summary: str | None
    response: str
    action: str | None
    action_status: str | None
