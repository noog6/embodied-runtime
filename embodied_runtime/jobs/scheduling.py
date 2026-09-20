"""Lightweight timer for offering daily Job activation opportunities."""

import asyncio
from collections.abc import Awaitable, Callable
import inspect
import logging
import math
from typing import Any


LOGGER = logging.getLogger(__name__)


class ScheduledJobController:
    """Periodically ask the application to consider one scheduled occurrence."""

    def __init__(self, poll_seconds: float, offer: Callable[[], Any], *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if (isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float))
                or not math.isfinite(poll_seconds) or poll_seconds <= 0):
            raise ValueError("poll_seconds must be a positive number")
        self.poll_seconds = float(poll_seconds)
        self._offer = offer
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run(), name="job-scheduler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                result = self._offer()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("[JOBS] scheduler=check_failed")
            await self._sleep(self.poll_seconds)
