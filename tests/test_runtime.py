import asyncio
import unittest
from unittest.mock import patch

from embodied_runtime.app import ApplicationOptions, LifecycleState, RobotApplication
from embodied_runtime.cli import build_parser, format_summary
from embodied_runtime.events import ApplicationStarted, Event, EventBus
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile


class VirtualHardwareTests(unittest.TestCase):
    def test_lifecycle(self) -> None:
        hardware = VirtualHardwareBackend()
        self.assertFalse(hardware.is_running)
        hardware.start()
        self.assertTrue(hardware.is_running)
        hardware.stop()
        self.assertFalse(hardware.is_running)


class RecordingEventBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[Event, LifecycleState]] = []
        self.application: RobotApplication | None = None

    async def publish(self, event: Event) -> None:
        assert self.application is not None
        self.published.append((event, self.application.state))
        await super().publish(event)


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.hardware = VirtualHardwareBackend()
        self.events = RecordingEventBus()
        self.application = RobotApplication(
            RobotProfile("test", "Test Robot"), self.hardware,
            ApplicationOptions(startup_prompt="private prompt"), self.events,
        )
        self.events.application = self.application

    async def test_start_and_stop_owns_event_bus_lifecycle(self) -> None:
        await self.application.start()
        self.assertEqual(self.application.state, LifecycleState.RUNNING)
        self.assertTrue(self.hardware.is_running)
        self.assertTrue(self.events.is_running)
        await self.application.stop()
        self.assertEqual(self.application.state, LifecycleState.STOPPED)
        self.assertFalse(self.hardware.is_running)
        self.assertFalse(self.events.is_running)

    async def test_started_event_announces_authoritative_running_state(self) -> None:
        await self.application.start()
        self.assertEqual(self.application.state, LifecycleState.RUNNING)
        await self.application.stop()
        self.assertEqual(self.application.state, LifecycleState.STOPPED)
        self.assertEqual(
            [(type(event), state) for event, state in self.events.published],
            [(ApplicationStarted, LifecycleState.RUNNING)],
        )

    async def test_full_event_queue_cannot_block_application_stop(self) -> None:
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()

        async def blocked_handler(event: ApplicationStarted) -> None:
            handler_started.set()
            await release_handler.wait()

        self.events.subscribe(ApplicationStarted, blocked_handler)
        await self.application.start()
        await handler_started.wait()
        for _ in range(64):
            await self.events.publish(ApplicationStarted(source="test"))

        await asyncio.wait_for(self.application.stop(), timeout=1)
        self.assertEqual(self.application.state, LifecycleState.STOPPED)
        self.assertFalse(self.hardware.is_running)
        self.assertFalse(self.events.is_running)

    async def test_startup_failure_stops_event_bus(self) -> None:
        with patch.object(self.hardware, "start", side_effect=RuntimeError("failed")):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                await self.application.start()
        self.assertEqual(self.application.state, LifecycleState.STOPPED)
        self.assertFalse(self.events.is_running)

    async def test_diagnostics_summary_omits_prompt_contents(self) -> None:
        with self.assertLogs("embodied_runtime.app", level="INFO") as logs:
            await self.application.start()
            rendered = format_summary(self.application.summary())
            await self.application.stop()
        self.assertEqual(rendered, "[DIAG] profile=test name='Test Robot' hardware=virtual "
                         "physical=false capabilities=none startup_prompt_provided=true lifecycle=running")
        self.assertNotIn("private prompt", rendered)
        self.assertNotIn("private prompt", "\n".join(logs.output))

    async def test_interruption_logs_and_cleans_up(self) -> None:
        async def interrupted_wait() -> None:
            raise asyncio.CancelledError

        with patch.object(self.application._stop_requested, "wait", interrupted_wait):
            with self.assertLogs("embodied_runtime.app", level="INFO") as logs:
                with self.assertRaises(asyncio.CancelledError):
                    await self.application.run()
        self.assertTrue(any(message.endswith("[APP] interrupted") for message in logs.output))
        self.assertEqual(self.application.state, LifecycleState.STOPPED)
        self.assertFalse(self.hardware.is_running)
        self.assertFalse(self.events.is_running)

    async def test_stopped_log_follows_event_bus_shutdown(self) -> None:
        bus_running_when_logged: list[bool] = []

        def record_log(message: str, *args: object) -> None:
            if message == "[APP] stopped":
                bus_running_when_logged.append(self.events.is_running)

        await self.application.start()
        with patch("embodied_runtime.app.LOGGER.info", side_effect=record_log):
            await self.application.stop()
        self.assertEqual(bus_running_when_logged, [False])


class CliTests(unittest.TestCase):
    def test_defaults(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(args.profile, "mira")
        self.assertEqual(args.hardware, "virtual")
        self.assertIsNone(args.startup_prompt)

    def test_optional_startup_prompt(self) -> None:
        args = build_parser().parse_args(["Good morning, Mira."])
        self.assertEqual(args.startup_prompt, "Good morning, Mira.")
