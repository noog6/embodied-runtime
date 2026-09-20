"""Transient results for explicitly invoked bounded Job work."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from embodied_runtime.jobs.continuation import (
    JobContinuationReadiness, JobReadinessEventType,
)
from embodied_runtime.jobs.progress import JobProgressUpdate


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
    readiness: JobContinuationReadiness | None = None
    delay_seconds: int | None = None
    event_type: JobReadinessEventType | None = None
    progress_update: JobProgressUpdate | None = None
