"""Small asyncio publish/subscribe mechanism for runtime events."""

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import Generic, TypeVar

from embodied_runtime.events.base import Event

LOGGER = logging.getLogger(__name__)

EventT = TypeVar("EventT", bound=Event)
EventHandler = Callable[[EventT], Awaitable[None]]


class Subscription(Generic[EventT]):
    """A single typed handler and its isolated, ordered delivery queue."""

    def __init__(
        self,
        bus: "EventBus",
        event_type: type[EventT],
        handler: EventHandler[EventT],
        queue_size: int,
    ) -> None:
        self._bus = bus
        self.event_type = event_type
        self._handler = handler
        self._queue: asyncio.Queue[EventT] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._closed_event = asyncio.Event()

    async def close(self) -> None:
        """Remove this subscription and stop any active delivery worker."""
        if self._closed:
            return
        self._closed = True
        self._closed_event.set()
        self._bus._remove(self)
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def _start(self) -> None:
        if not self._closed and self._task is None:
            self._task = asyncio.create_task(
                self._deliver(),
                name=f"event:{self.event_type.__name__}:{self._handler_name}",
            )

    async def _enqueue(self, event: EventT) -> None:
        if self._closed:
            raise RuntimeError("Subscription is closed")

        put = asyncio.create_task(self._queue.put(event))
        closed = asyncio.create_task(self._closed_event.wait())
        try:
            done, _ = await asyncio.wait(
                (put, closed), return_when=asyncio.FIRST_COMPLETED
            )
            if closed in done or self._closed:
                put.cancel()
                await asyncio.gather(put, return_exceptions=True)
                raise RuntimeError("Subscription closed before accepting event")
            await put
        finally:
            for task in (put, closed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(put, closed, return_exceptions=True)

    async def _deliver(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._handler(event)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.exception(
                    "[EVENT] handler_failed event=%s handler=%s error=%s",
                    type(event).__name__,
                    self._handler_name,
                    error,
                )
            finally:
                self._queue.task_done()

    @property
    def _handler_name(self) -> str:
        return getattr(self._handler, "__qualname__", repr(self._handler))


class EventBus:
    """An in-process, transient event bus with per-subscriber buffering."""

    def __init__(self, *, queue_size: int = 64) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size must be positive")
        self._queue_size = queue_size
        self._subscriptions: list[Subscription[Event]] = []
        self._running = False
        self._stopped = False

    @property
    def is_running(self) -> bool:
        return self._running

    def subscribe(
        self, event_type: type[EventT], handler: EventHandler[EventT]
    ) -> Subscription[EventT]:
        if not isinstance(event_type, type) or not issubclass(event_type, Event):
            raise TypeError("event_type must be an Event subclass")
        if self._stopped:
            raise RuntimeError("EventBus has been stopped")
        subscription = Subscription(self, event_type, handler, self._queue_size)
        self._subscriptions.append(subscription)  # type: ignore[arg-type]
        if self._running:
            subscription._start()
        return subscription

    async def start(self) -> None:
        if self._stopped:
            raise RuntimeError("EventBus cannot be restarted after stop")
        if self._running:
            raise RuntimeError("EventBus is already running")
        self._running = True
        for subscription in self._subscriptions:
            subscription._start()

    async def publish(self, event: Event) -> None:
        if not isinstance(event, Event):
            raise TypeError("event must be an Event instance")
        if not self._running:
            raise RuntimeError("EventBus is not running")
        matching = [
            subscription
            for subscription in self._subscriptions
            if isinstance(event, subscription.event_type)
        ]
        # Each put is independent of handler execution. A full bounded queue
        # deliberately applies backpressure rather than silently losing events.
        await asyncio.gather(*(subscription._enqueue(event) for subscription in matching))

    async def stop(self) -> None:
        if self._stopped:
            return
        self._running = False
        self._stopped = True
        subscriptions = tuple(self._subscriptions)
        for subscription in subscriptions:
            await subscription.close()
        self._subscriptions.clear()

    def _remove(self, subscription: Subscription[Event]) -> None:
        try:
            self._subscriptions.remove(subscription)
        except ValueError:
            pass
