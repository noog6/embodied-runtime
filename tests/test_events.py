import asyncio
from dataclasses import dataclass
import unittest

from embodied_runtime.events import Event, EventBus


@dataclass(frozen=True, slots=True, kw_only=True)
class SampleEvent(Event):
    value: int


class EventBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_requires_event_subclass(self) -> None:
        bus = EventBus()

        async def handler(event: SampleEvent) -> None:
            pass

        with self.assertRaisesRegex(TypeError, "Event subclass"):
            bus.subscribe(str, handler)  # type: ignore[arg-type]

    async def test_publish_requires_event_instance(self) -> None:
        bus = EventBus()
        await bus.start()
        with self.assertRaisesRegex(TypeError, "Event instance"):
            await bus.publish(object())  # type: ignore[arg-type]
        await bus.stop()

    async def test_publishes_to_two_subscribers(self) -> None:
        bus = EventBus()
        received = [[], []]
        done = [asyncio.Event(), asyncio.Event()]

        def make_handler(index: int):
            async def handler(event: SampleEvent) -> None:
                received[index].append(event.value)
                done[index].set()
            return handler

        bus.subscribe(SampleEvent, make_handler(0))
        bus.subscribe(SampleEvent, make_handler(1))
        await bus.start()
        await bus.publish(SampleEvent(source="test", value=1))
        await asyncio.gather(*(item.wait() for item in done))
        self.assertEqual(received, [[1], [1]])
        await bus.stop()

    async def test_delivery_is_ordered(self) -> None:
        bus = EventBus()
        received = []
        delivered = asyncio.Event()

        async def handler(event: SampleEvent) -> None:
            received.append(event.value)
            if len(received) == 3:
                delivered.set()

        bus.subscribe(SampleEvent, handler)
        await bus.start()
        for value in range(3):
            await bus.publish(SampleEvent(source="test", value=value))
        await delivered.wait()
        self.assertEqual(received, [0, 1, 2])
        await bus.stop()

    async def test_slow_subscriber_does_not_block_other_worker(self) -> None:
        bus = EventBus(queue_size=2)
        release_slow = asyncio.Event()
        slow_started = asyncio.Event()
        fast_finished = asyncio.Event()

        async def slow_handler(event: SampleEvent) -> None:
            slow_started.set()
            await release_slow.wait()

        async def fast_handler(event: SampleEvent) -> None:
            fast_finished.set()

        bus.subscribe(SampleEvent, slow_handler)
        bus.subscribe(SampleEvent, fast_handler)
        await bus.start()
        await bus.publish(SampleEvent(source="test", value=1))
        await slow_started.wait()
        await fast_finished.wait()
        self.assertFalse(release_slow.is_set())
        release_slow.set()
        await bus.stop()

    async def test_handler_failure_is_logged_and_worker_continues(self) -> None:
        bus = EventBus()
        attempted = []
        later_delivered = asyncio.Event()

        async def unreliable(event: SampleEvent) -> None:
            attempted.append(event.value)
            if event.value == 1:
                raise ValueError("broken")
            later_delivered.set()

        bus.subscribe(SampleEvent, unreliable)
        await bus.start()
        with self.assertLogs("embodied_runtime.events.bus", level="ERROR") as logs:
            await bus.publish(SampleEvent(source="test", value=1))
            await bus.publish(SampleEvent(source="test", value=2))
            await later_delivered.wait()
        self.assertEqual(attempted, [1, 2])
        self.assertIn("[EVENT] handler_failed event=SampleEvent", "\n".join(logs.output))
        await bus.stop()

    async def test_close_prevents_future_delivery(self) -> None:
        bus = EventBus()
        received = []

        async def handler(event: SampleEvent) -> None:
            received.append(event.value)

        subscription = bus.subscribe(SampleEvent, handler)
        await bus.start()
        await subscription.close()
        await bus.publish(SampleEvent(source="test", value=1))
        await asyncio.sleep(0)
        self.assertEqual(received, [])
        await bus.stop()

    async def test_publish_requires_running_bus(self) -> None:
        bus = EventBus()
        with self.assertRaisesRegex(RuntimeError, "not running"):
            await bus.publish(SampleEvent(source="test", value=1))
        await bus.start()
        await bus.stop()
        with self.assertRaisesRegex(RuntimeError, "not running"):
            await bus.publish(SampleEvent(source="test", value=2))

    async def test_stop_releases_workers(self) -> None:
        bus = EventBus()

        async def handler(event: SampleEvent) -> None:
            pass

        subscriptions = [bus.subscribe(SampleEvent, handler) for _ in range(2)]
        await bus.start()
        tasks = [subscription._task for subscription in subscriptions]
        self.assertTrue(all(task is not None and not task.done() for task in tasks))
        await bus.stop()
        self.assertTrue(all(task is not None and task.done() for task in tasks))

    async def test_close_releases_publish_blocked_by_full_queue(self) -> None:
        bus = EventBus(queue_size=1)
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()

        async def handler(event: SampleEvent) -> None:
            handler_started.set()
            await release_handler.wait()

        subscription = bus.subscribe(SampleEvent, handler)
        await bus.start()
        await bus.publish(SampleEvent(source="test", value=1))
        await handler_started.wait()
        await bus.publish(SampleEvent(source="test", value=2))
        blocked_publish = asyncio.create_task(
            bus.publish(SampleEvent(source="test", value=3))
        )
        while not subscription._queue._putters:
            await asyncio.sleep(0)

        await subscription.close()
        with self.assertRaisesRegex(RuntimeError, "closed before accepting"):
            await asyncio.wait_for(blocked_publish, timeout=1)
        await bus.stop()

    async def test_stop_releases_publish_blocked_by_full_queue(self) -> None:
        bus = EventBus(queue_size=1)
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()

        async def handler(event: SampleEvent) -> None:
            handler_started.set()
            await release_handler.wait()

        subscription = bus.subscribe(SampleEvent, handler)
        await bus.start()
        await bus.publish(SampleEvent(source="test", value=1))
        await handler_started.wait()
        await bus.publish(SampleEvent(source="test", value=2))
        blocked_publish = asyncio.create_task(
            bus.publish(SampleEvent(source="test", value=3))
        )
        while not subscription._queue._putters:
            await asyncio.sleep(0)

        await asyncio.wait_for(bus.stop(), timeout=1)
        with self.assertRaisesRegex(RuntimeError, "closed before accepting"):
            await asyncio.wait_for(blocked_publish, timeout=1)

    async def test_full_queue_preserves_backpressure_and_order(self) -> None:
        bus = EventBus(queue_size=1)
        received = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        all_delivered = asyncio.Event()

        async def handler(event: SampleEvent) -> None:
            received.append(event.value)
            if event.value == 1:
                first_started.set()
                await release_first.wait()
            if len(received) == 3:
                all_delivered.set()

        subscription = bus.subscribe(SampleEvent, handler)
        await bus.start()
        await bus.publish(SampleEvent(source="test", value=1))
        await first_started.wait()
        await bus.publish(SampleEvent(source="test", value=2))
        blocked_publish = asyncio.create_task(
            bus.publish(SampleEvent(source="test", value=3))
        )
        while not subscription._queue._putters:
            await asyncio.sleep(0)
        self.assertFalse(blocked_publish.done())

        release_first.set()
        await blocked_publish
        await all_delivered.wait()
        self.assertEqual(received, [1, 2, 3])
        await bus.stop()
