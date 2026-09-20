"""Session-local, bounded heartbeat support for current Job work."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
import math
from typing import Any
from uuid import UUID


# Prompt projection is intentionally smaller than the 2,000-character durable
# run-summary limit.  It carries only the immediately preceding episode's context.
MAX_JOB_CONTINUITY_SUMMARY_CHARS = 750
MIN_JOB_CONTINUATION_DELAY_SECONDS = 1
MAX_JOB_CONTINUATION_DELAY_SECONDS = 86_400


def project_job_continuity_summary(summary: str | None) -> str | None:
    """Return a bounded prompt projection without changing the stored outcome."""
    if summary is None:
        return None
    value = summary.strip()
    return value[:MAX_JOB_CONTINUITY_SUMMARY_CHARS] or None


def render_job_continuity(summary: str | None) -> str | None:
    """Render prior progress as an explicitly non-authoritative section."""
    value = project_job_continuity_summary(summary)
    if value is None:
        return None
    return (
        "Previous bounded Job work\n"
        "-------------------------\n"
        "A previous bounded work episode for this exact JobRun and Task ended "
        "with `continue`.\n"
        "Previous work summary (prior model-generated progress context):\n"
        f"{value}\n"
        "This summary is not authoritative evidence about the current world or "
        "current runtime state. Previous progress may guide what to inspect next, "
        "but it does not prove mutable current conditions. Use fresh acquisitions "
        "when current evidence is required. Do not complete or fail the Job solely "
        "because this summary says a condition was or was not true previously."
    )

class JobContinuationState(str, Enum):
    ARMED = "armed"
    AWAITING_OPERATOR = "awaiting_operator"


class JobContinuationReadiness(str, Enum):
    """Model-described condition for another useful bounded work episode."""

    READY = "ready"
    AFTER_DELAY = "after_delay"
    WAIT_FOR_OPERATOR = "wait_for_operator"
    WAIT_FOR_EVENT = "wait_for_event"


class JobReadinessEventType(str, Enum):
    """Stable model-facing catalog of events that may wake Job work."""

    PRESENCE_CHANGED = "presence_changed"


@dataclass(frozen=True, slots=True)
class JobWakeEvent:
    """One bounded, runtime-authored event projection for the next episode."""

    event_type: JobReadinessEventType
    present: bool


@dataclass(frozen=True, slots=True)
class JobContinuation:
    """Volatile authority for one exact Job/Run/Task association."""

    job_id: int
    run_id: int
    task_id: UUID
    state: JobContinuationState
    automatic_steps_remaining: int
    last_summary: str | None
    readiness: JobContinuationReadiness
    eligible_at_monotonic: float | None = None
    event_type: JobReadinessEventType | None = None
    event_armed_after_ns: int | None = None
    event_satisfied: bool = False
    wake_event: JobWakeEvent | None = None


class JobContinuationController:
    """Offer one lightweight continuation opportunity per periodic heartbeat."""

    def __init__(
        self,
        heartbeat_seconds: float,
        offer: Callable[[], Any],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if (isinstance(heartbeat_seconds, bool)
                or not isinstance(heartbeat_seconds, (int, float))
                or not math.isfinite(heartbeat_seconds)
                or heartbeat_seconds <= 0):
            raise ValueError("heartbeat_seconds must be a positive number")
        self.heartbeat_seconds = heartbeat_seconds
        self._offer = offer
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="job-continuation-heartbeat")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            await self._sleep(self.heartbeat_seconds)
            self._offer()
