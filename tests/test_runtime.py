import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from embodied_runtime.app import ApplicationOptions, LifecycleState, RobotApplication
from embodied_runtime.cli import build_cognition_backend, build_hardware_backend, build_parser, format_platform, format_summary, main
from embodied_runtime.cognition.openai_responses import OpenAIResponsesBackend
from embodied_runtime.hardware.fusion_hat import (
    FusionHatHardwareBackend,
    FusionHatUnavailableError,
)
from embodied_runtime.events import ApplicationStarted, Event, EventBus
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile
from tests.test_platform import snapshot


class FakePlatformProvider:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)

    def snapshot(self):
        return next(self.snapshots)


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
        self.first_platform = snapshot(hostname="first")
        self.second_platform = snapshot(hostname="second", captured_monotonic=2.0)
        self.platform_provider = FakePlatformProvider(
            [self.first_platform, self.second_platform]
        )
        self.application = RobotApplication(
            RobotProfile("test", "Test Robot"), self.hardware,
            ApplicationOptions(startup_prompt="private prompt"), self.events,
            self.platform_provider,
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

    async def test_start_captures_platform_state(self) -> None:
        await self.application.start()
        self.assertIs(self.application.runtime_state.platform, self.first_platform)
        await self.application.stop()

    async def test_refresh_replaces_platform_snapshot(self) -> None:
        await self.application.start()
        previous_state = self.application.runtime_state
        refreshed = self.application.refresh_platform_state()
        self.assertIs(refreshed, self.second_platform)
        self.assertIs(self.application.runtime_state.platform, self.second_platform)
        self.assertIs(previous_state.platform, self.first_platform)
        await self.application.stop()

    async def test_runtime_state_cannot_be_mutated(self) -> None:
        await self.application.start()
        with self.assertRaises(FrozenInstanceError):
            self.application.runtime_state.lifecycle = LifecycleState.STOPPED  # type: ignore[misc]
        await self.application.stop()

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
        self.assertEqual(args.cognition, "none")
        self.assertFalse(args.initiative)
        self.assertFalse(args.initiative_actions)
        self.assertFalse(args.initiative_messages)
        self.assertFalse(args.initiative_continuation)
        self.assertFalse(args.initiative_goal_closure)

    def test_initiative_requires_cognition_backend(self) -> None:
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            main(["--initiative"])

    def test_initiative_actions_requires_explicit_initiative(self) -> None:
        for argv in (
            ["--initiative-actions", "--console"],
            ["--cognition", "openai-responses", "--initiative-actions", "--console"],
        ):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit):
                main(argv)

    def test_initiative_messages_requires_initiative_and_console(self) -> None:
        for argv in (
            ["--initiative-messages", "--console"],
            ["--cognition", "openai-responses", "--initiative-messages", "--console"],
            ["--cognition", "openai-responses", "--initiative",
             "--initiative-messages"],
        ):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit):
                main(argv)

    def test_initiative_goal_closure_requires_actions(self) -> None:
        for argv in (
            ["--initiative-goal-closure", "--console"],
            ["--cognition", "openai-responses", "--initiative",
             "--initiative-goal-closure", "--console"],
        ):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit):
                main(argv)

    def test_initiative_continuation_requires_all_effect_permissions(self) -> None:
        for argv in (
            ["--initiative-continuation", "--console"],
            ["--cognition", "openai-responses", "--initiative",
             "--initiative-continuation", "--console"],
            ["--cognition", "openai-responses", "--initiative",
             "--initiative-actions", "--initiative-continuation", "--console"],
            ["--cognition", "openai-responses", "--initiative",
             "--initiative-messages", "--initiative-continuation", "--console"],
        ):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit):
                main(argv)

    def test_openai_cognition_selection_is_lazy(self) -> None:
        args = build_parser().parse_args(["--cognition", "openai-responses"])
        with patch.object(OpenAIResponsesBackend, "_get_client") as get_client:
            backend = build_cognition_backend(args)
        self.assertIsInstance(backend, OpenAIResponsesBackend)
        get_client.assert_not_called()

    def test_optional_startup_prompt(self) -> None:
        args = build_parser().parse_args(["Good morning, Mira."])
        self.assertEqual(args.startup_prompt, "Good morning, Mira.")

    def test_explicit_fusion_hat_builds_physical_backend(self) -> None:
        args = build_parser().parse_args(["--hardware", "fusion-hat"])
        self.assertIsInstance(build_hardware_backend(args), FusionHatHardwareBackend)

    def test_servo_test_requires_diagnostics_and_physical_hardware(self) -> None:
        for argv in (["--fusion-servo-test", "P0"],
                     ["--diagnostics", "--fusion-servo-test", "P0"]):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit):
                main(argv)

    def test_invalid_servo_channel_is_rejected(self) -> None:
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            main(["--hardware", "fusion-hat", "--diagnostics", "--fusion-servo-test", "P12"])

    def test_missing_physical_driver_is_concise_without_fallback(self) -> None:
        error = FusionHatUnavailableError(
            "Fusion HAT unavailable; run `fusion_hat doctor`"
        )
        with patch.object(FusionHatHardwareBackend, "start", side_effect=error):
            with patch("sys.stderr") as stderr:
                self.assertEqual(
                    main(["--hardware", "fusion-hat", "--diagnostics"]), 2
                )
        rendered = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("fusion_hat doctor", rendered)

    def test_platform_diagnostics_are_structured(self) -> None:
        rendered = format_platform(
            snapshot(
                model="Test Model",
                uptime_seconds=123.456,
                load_averages=(0.12, 0.2, 0.3),
                memory_total_bytes=512 * 1024 * 1024,
                memory_available_bytes=350 * 1024 * 1024,
                cpu_temperature_celsius=42.75,
            )
        )
        self.assertEqual(
            rendered,
            "[PLATFORM] hostname=test-host system=TestOS release=1 machine=test64 "
            "python=3.13.5 model='Test Model' uptime_s=123.5 load_1m=0.12 "
            "memory_available_mb=350 memory_total_mb=512 cpu_temp_c=42.8",
        )

    def test_missing_platform_metrics_are_unknown(self) -> None:
        rendered = format_platform(snapshot())
        self.assertIn("model='unknown'", rendered)
        self.assertIn("uptime_s=unknown", rendered)
        self.assertIn("load_1m=unknown", rendered)
        self.assertIn("memory_available_mb=unknown", rendered)
        self.assertIn("cpu_temp_c=unknown", rendered)
        self.assertNotIn("None", rendered)
