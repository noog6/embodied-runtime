"""Session-local, bounded heartbeat support for current Job work."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
import math
from typing import Any
from uuid import UUID


class JobContinuationState(str, Enum):
    ARMED = "armed"
    AWAITING_OPERATOR = "awaiting_operator"


@dataclass(frozen=True, slots=True)
class JobContinuation:
    """Volatile authority for one exact Job/Run/Task association."""

    job_id: int
    run_id: int
    task_id: UUID
    state: JobContinuationState
    automatic_steps_remaining: int
    last_summary: str | None


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
