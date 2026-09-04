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
    """Own one temporal commitment, pending as a timer or a due handoff."""

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
        self._due_pending = False
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
        self._due_pending = False
        self._task = asyncio.create_task(
            self._wait(pending), name="temporal:followup"
        )
        LOGGER.info("[TEMPORAL] status=scheduled delay_s=%s", delay_seconds)
        return pending

    def cancel(self, reason: str) -> bool:
        if self._pending is None:
            return False
        self._pending = None
        self._due_pending = False
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
        if self._due_pending:
            return TemporalFollowupStatus(
                "due_pending", pending.delay_seconds, 0, pending.purpose
            )
        remaining = max(0, min(
            pending.delay_seconds,
            int(max(0.0, pending.due_monotonic - self._monotonic()) + 0.999999),
        ))
        return TemporalFollowupStatus(
            "pending", pending.delay_seconds, remaining, pending.purpose
        )

    def claim_due(
        self, goal: ActiveGoal | None = None, *, delay_seconds: int | None = None,
        purpose: str | None = None,
    ) -> TemporalFollowupDue | None:
        """Atomically release the due slot for one exact-goal attention episode."""
        pending = self._pending
        if not self._due_pending or pending is None:
            return None
        if goal is not None and pending.goal is not goal:
            return None
        if delay_seconds is not None and pending.delay_seconds != delay_seconds:
            return None
        if purpose is not None and pending.purpose != purpose:
            return None
        if not self._is_running() or self._current_goal() is not pending.goal:
            self.cancel("goal_changed")
            return None
        self._pending = None
        self._due_pending = False
        return TemporalFollowupDue(
            source="temporal_followup", purpose=pending.purpose,
            delay_seconds=pending.delay_seconds, bound_goal=pending.goal,
        )

    async def _wait(self, pending: PendingFollowup) -> None:
        try:
            await self._sleep(pending.delay_seconds)
        except asyncio.CancelledError:
            return
        if self._pending is not pending:
            return
        self._task = None
        if not self._is_running():
            LOGGER.info("[TEMPORAL] status=cancelled reason=shutdown")
            return
        if self._current_goal() is not pending.goal:
            LOGGER.info("[TEMPORAL] status=cancelled reason=goal_changed")
            return
        self._due_pending = True
        LOGGER.info("[TEMPORAL] status=due")
        await self._events.publish(TemporalFollowupDue(
            source="temporal_followup", purpose=pending.purpose,
            delay_seconds=pending.delay_seconds, bound_goal=pending.goal,
        ))
