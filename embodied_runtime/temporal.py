"""One-shot, session-local temporal follow-up ownership."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from time import monotonic

from embodied_runtime.cognition import ActiveGoal
from embodied_runtime.events import EventBus, TemporalFollowupDue

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PendingFollowup:
    """A single future attention commitment bound to an exact goal object."""

    delay_seconds: int
    purpose: str
    goal: ActiveGoal
    due_monotonic: float


@dataclass(frozen=True, slots=True)
class TemporalFollowupStatus:
    state: str
    delay_seconds: int | None = None
    remaining_seconds: int | None = None
    purpose: str | None = None


class TemporalFollowupController:
    """Own at most one monotonic timer; this is deliberately not a scheduler."""

    def __init__(
        self, events: EventBus, *, is_running: Callable[[], bool],
        current_goal: Callable[[], ActiveGoal | None],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._events = events
        self._is_running = is_running
        self._current_goal = current_goal
        self._sleep = sleep
        self._monotonic = monotonic_clock
        self._pending: PendingFollowup | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def pending(self) -> PendingFollowup | None:
        return self._pending

    def schedule(self, delay_seconds: int, purpose: str, goal: ActiveGoal) -> PendingFollowup:
        if self._pending is not None:
            raise RuntimeError("a temporal follow-up is already pending")
        pending = PendingFollowup(
            delay_seconds, purpose, goal, self._monotonic() + delay_seconds
        )
        self._pending = pending
        self._task = asyncio.create_task(
            self._wait(pending), name="temporal:followup"
        )
        LOGGER.info("[TEMPORAL] status=scheduled delay_s=%s", delay_seconds)
        return pending

    def cancel(self, reason: str) -> bool:
        if self._pending is None:
            return False
        self._pending = None
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
        LOGGER.info("[TEMPORAL] status=cancelled reason=%s", reason)
        return True

    async def stop(self) -> None:
        task = self._task
        self.cancel("shutdown")
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def status(self) -> TemporalFollowupStatus:
        pending = self._pending
        if pending is None:
            return TemporalFollowupStatus("none")
        remaining = max(0, min(
            pending.delay_seconds,
            int(max(0.0, pending.due_monotonic - self._monotonic()) + 0.999999),
        ))
        return TemporalFollowupStatus(
            "pending", pending.delay_seconds, remaining, pending.purpose
        )

    async def _wait(self, pending: PendingFollowup) -> None:
        try:
            await self._sleep(pending.delay_seconds)
        except asyncio.CancelledError:
            return
        if self._pending is not pending:
            return
        self._pending = None
        self._task = None
        if not self._is_running():
            LOGGER.info("[TEMPORAL] status=cancelled reason=shutdown")
            return
        if self._current_goal() is not pending.goal:
            LOGGER.info("[TEMPORAL] status=cancelled reason=goal_changed")
            return
        LOGGER.info("[TEMPORAL] status=due")
        await self._events.publish(TemporalFollowupDue(
            source="temporal_followup", purpose=pending.purpose,
            delay_seconds=pending.delay_seconds, bound_goal=pending.goal,
        ))
