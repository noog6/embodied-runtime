import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest import mock
from unittest.mock import AsyncMock, patch

from embodied_runtime.app import ApplicationOptions, LifecycleState, RobotApplication
from embodied_runtime.cli import (
    _live_non_daemon_thread_names, _run_console_application,
    _run_application, _run_headless_application, _run_process_application,
    _run_with_asyncio_cleanup,
    build_cognition_backend, build_hardware_backend,
    build_parser, format_platform, format_summary, main, parse_launch_arguments,
)
from embodied_runtime.cognition.openai_responses import OpenAIResponsesBackend
from embodied_runtime.hardware.fusion_hat import (
    FusionHatHardwareBackend,
    FusionHatUnavailableError,
)
from embodied_runtime.hardware.host import HostHardwareBackend
from embodied_runtime.events import ApplicationStarted, Event, EventBus
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile
from embodied_runtime.sensing.camera.picamera2 import Picamera2DeviceUnavailableError
from tests.test_platform import snapshot


class FakePlatformProvider:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)

    def snapshot(self):
        return next(self.snapshots)


class CountingMemoryStore:
    def __init__(self, close_error: Exception | None = None):
        self.close_calls = 0
        self.close_error = close_error

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


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
        self.third_platform = snapshot(hostname="third", captured_monotonic=3.0)
        self.platform_provider = FakePlatformProvider(
            [self.first_platform, self.second_platform, self.third_platform]
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
        self.assertIs(self.application.runtime_state.platform, self.second_platform)
        self.assertIsNone(self.application.runtime_state.power.battery_voltage_v)
        await self.application.stop()

    async def test_refresh_replaces_platform_snapshot(self) -> None:
        await self.application.start()
        previous_state = self.application.runtime_state
        refreshed = self.application.refresh_platform_state()
        self.assertIs(refreshed, self.third_platform)
        self.assertIs(self.application.runtime_state.platform, self.third_platform)
        self.assertIs(previous_state.platform, self.second_platform)
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

    async def test_startup_failure_closes_memory_once_and_preserves_error(self) -> None:
        memory = CountingMemoryStore(RuntimeError("memory close failed"))
        application = RobotApplication(
            RobotProfile("test", "Test Robot"), self.hardware,
            events=self.events, platform_provider=self.platform_provider,
            persistent_memory_store=memory,
        )
        with patch.object(
            self.hardware, "start", side_effect=RuntimeError("hardware failed")
        ), self.assertLogs("embodied_runtime.app", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "hardware failed"):
                await application.start()
        self.assertEqual(application.state, LifecycleState.STOPPED)
        self.assertFalse(self.events.is_running)
        self.assertEqual(memory.close_calls, 1)
        await application.stop()
        self.assertEqual(memory.close_calls, 1)

    async def test_successful_shutdown_closes_memory_once(self) -> None:
        memory = CountingMemoryStore()
        application = RobotApplication(
            RobotProfile("test", "Test Robot"), self.hardware,
            events=self.events, platform_provider=self.platform_provider,
            persistent_memory_store=memory,
        )
        await application.start()
        await application.stop()
        await application.stop()
        self.assertEqual(memory.close_calls, 1)

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

    async def test_console_cancellation_logs_lifecycle_once(self) -> None:
        class WaitingTerminal:
            def write(self, _text):
                pass

            async def read_line(self, _prompt):
                await asyncio.Event().wait()

        with self.assertLogs(level="INFO") as captured:
            task = asyncio.create_task(_run_console_application(
                self.application, WaitingTerminal(), None  # type: ignore[arg-type]
            ))
            while self.application.state is not LifecycleState.RUNNING:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        lifecycle = [line.rsplit(" ", 1)[-1] for line in captured.output
                     if "[APP]" in line]
        self.assertEqual(lifecycle.count("interrupted"), 1)
        self.assertEqual(lifecycle.count("stopping"), 1)
        self.assertEqual(lifecycle.count("stopped"), 1)
        self.assertLess(lifecycle.index("interrupted"), lifecycle.index("stopping"))
        self.assertLess(lifecycle.index("stopping"), lifecycle.index("stopped"))

    async def test_console_quit_logs_stop_without_interrupt(self) -> None:
        class QuitTerminal:
            def write(self, _text):
                pass

            async def read_line(self, _prompt):
                return "quit"

        with self.assertLogs(level="INFO") as captured:
            result = await _run_console_application(
                self.application, QuitTerminal(), None  # type: ignore[arg-type]
            )
        self.assertEqual(result, 0)
        logs = "\n".join(captured.output)
        self.assertNotIn("[APP] interrupted", logs)
        self.assertEqual(logs.count("[APP] stopping"), 1)
        self.assertEqual(logs.count("[APP] stopped"), 1)

    async def test_console_eof_stops_normally_once(self) -> None:
        terminal = SimpleNamespace(
            write=lambda _text: None,
            read_line=AsyncMock(return_value=None),
        )
        with self.assertLogs(level="INFO") as captured:
            result = await _run_console_application(
                self.application, terminal, None  # type: ignore[arg-type]
            )
        logs = "\n".join(captured.output)
        self.assertEqual(result, 0)
        self.assertNotIn("[APP] interrupted", logs)
        self.assertEqual(logs.count("[APP] stopping"), 1)
        self.assertEqual(logs.count("[APP] stopped"), 1)


class HeadlessLifecycleTests(unittest.IsolatedAsyncioTestCase):
    class ControlledApplication(RobotApplication):
        def __init__(self, *, block_startup: bool):
            super().__init__(
                RobotProfile("test", "Test Robot"), VirtualHardwareBackend(),
                ApplicationOptions(),
            )
            self.block_startup = block_startup
            self.start_entered = asyncio.Event()
            self.start_release = asyncio.Event()
            self.stop_reached = asyncio.Event()
            self.start_calls = 0
            self.stop_calls = 0
            self.request_stop_calls = 0
            self.start_cancelled = False

        async def start(self):
            self.start_calls += 1
            self._set_lifecycle(LifecycleState.STARTING)
            self.start_entered.set()
            if self.block_startup:
                try:
                    await self.start_release.wait()
                except asyncio.CancelledError:
                    self.start_cancelled = True
                    raise
            self._set_lifecycle(LifecycleState.RUNNING)

        async def stop(self):
            self.stop_calls += 1
            self._set_lifecycle(LifecycleState.STOPPED)
            self.stop_reached.set()

        def request_stop(self):
            self.request_stop_calls += 1
            super().request_stop()

    async def test_sigterm_during_startup_cancels_start_and_reaches_stop(self):
        application = self.ControlledApplication(block_startup=True)
        shutdown = asyncio.Event()
        task = asyncio.create_task(_run_headless_application(application, shutdown))
        await asyncio.wait_for(application.start_entered.wait(), timeout=1)
        shutdown.set()
        with patch("embodied_runtime.app.LOGGER.info") as log:
            result = await asyncio.wait_for(task, timeout=1)
        self.assertEqual(result, 0)
        self.assertTrue(application.start_cancelled)
        self.assertTrue(application.stop_reached.is_set())
        self.assertEqual(application.start_calls, 1)
        self.assertEqual(application.stop_calls, 1)
        self.assertEqual(application.request_stop_calls, 1)
        self.assertNotIn(
            "[APP] interrupted", [call.args[0] for call in log.call_args_list]
        )

    async def test_sigterm_after_running_requests_one_ordinary_stop(self):
        application = self.ControlledApplication(block_startup=False)
        shutdown = asyncio.Event()
        task = asyncio.create_task(_run_headless_application(application, shutdown))
        await asyncio.wait_for(application.start_entered.wait(), timeout=1)
        while application.state is not LifecycleState.RUNNING:
            await asyncio.sleep(0)
        shutdown.set()
        shutdown.set()
        result = await asyncio.wait_for(task, timeout=1)
        self.assertEqual(result, 0)
        self.assertFalse(application.start_cancelled)
        self.assertEqual(application.start_calls, 1)
        self.assertEqual(application.request_stop_calls, 1)
        self.assertEqual(application.stop_calls, 1)

    async def test_signal_handler_is_installed_only_for_run_mode(self):
        profile = RobotProfile("test", "Test Robot")
        placeholders = (mock.Mock(), mock.Mock())
        for mode in ("console", "diagnostics"):
            args = SimpleNamespace(mode=mode)
            with self.subTest(mode=mode), patch(
                "embodied_runtime.cli._run_application", new=AsyncMock(return_value=0)
            ) as run, patch(
                "embodied_runtime.cli.asyncio.get_running_loop",
                side_effect=AssertionError("non-run mode must not install a handler"),
            ):
                self.assertEqual(await _run_process_application(
                    args, profile, mock.Mock(), *placeholders,
                ), 0)
                self.assertIsNone(run.await_args.args[5] if len(run.await_args.args) > 5 else None)

        loop = asyncio.get_running_loop()
        callbacks = []

        async def run_mode(*args):
            shutdown = args[5]
            callbacks[0]()
            callbacks[0]()
            self.assertTrue(shutdown.is_set())
            return 0

        with patch.object(
            loop, "add_signal_handler",
            side_effect=lambda _signal, callback: callbacks.append(callback),
        ) as add_handler, patch.object(
            loop, "remove_signal_handler", return_value=True,
        ) as remove_handler, patch(
            "embodied_runtime.cli._run_application", side_effect=run_mode,
        ), self.assertLogs("embodied_runtime.cli", level="INFO") as captured:
            self.assertEqual(await _run_process_application(
                SimpleNamespace(mode="run"), profile, mock.Mock(), *placeholders,
            ), 0)
        add_handler.assert_called_once()
        remove_handler.assert_called_once()
        self.assertEqual(
            "\n".join(captured.output).count(
                "signal=SIGTERM action=shutdown_requested"
            ), 1,
        )

    async def test_headless_sms_composition_has_no_console_infrastructure(self):
        _, args, _ = parse_launch_arguments(["--mode", "run", "--sms"])
        application = mock.Mock()
        application.run = AsyncMock(return_value=None)
        environment = {
            "TWILIO_ACCOUNT_SID": "AC00000000000000000000000000000000",
            "TWILIO_AUTH_TOKEN": "secret",
            "TWILIO_PHONE_NUMBER": "+15550000001",
            "MIRA_SMS_OPERATOR_NUMBER": "+15550000002",
            "TWILIO_WEBHOOK_URL": "https://example.invalid/sms",
        }
        with patch.dict("os.environ", environment, clear=True), patch(
            "embodied_runtime.cli.RobotApplication", return_value=application,
        ) as constructor, patch(
            "embodied_runtime.cli.AsyncLineTerminal"
        ) as terminal, patch("embodied_runtime.cli.RuntimeConsole") as console:
            result = await _run_application(
                args, RobotProfile("test", "Test Robot"),
            )
        self.assertEqual(result, 0)
        application.run.assert_awaited_once()
        terminal.assert_not_called()
        console.assert_not_called()
        routes = constructor.call_args.kwargs["operator_delivery_routes"]
        self.assertEqual(
            [(item.name, item.channel.value) for item in routes.destinations],
            [("sms", "remote_text")],
        )

    async def test_body_and_fusion_audio_composition_follow_resolved_backend(self):
        profile = RobotProfile("mira", "Mira")
        cases = (
            ("host", HostHardwareBackend, False, False),
            ("virtual", VirtualHardwareBackend, True, False),
            ("fusion-hat", FusionHatHardwareBackend, False, True),
        )
        for selection, backend_type, has_body, has_fusion_audio in cases:
            with self.subTest(selection=selection):
                _, args, _ = parse_launch_arguments([
                    "--mode", "run", "--hardware", selection, "--voice",
                ])
                application = mock.Mock()
                application.run = AsyncMock(return_value=None)
                with patch(
                    "embodied_runtime.cli.RobotApplication", return_value=application,
                ) as constructor:
                    self.assertEqual(await _run_application(args, profile), 0)
                positional = constructor.call_args.args
                composed = constructor.call_args.kwargs
                self.assertIsInstance(positional[1], backend_type)
                self.assertEqual(composed["body_backend"] is not None, has_body)
                self.assertEqual(composed["voice_provider"] is not None, has_fusion_audio)
                self.assertEqual(
                    composed["text_to_speech_provider"] is not None,
                    has_fusion_audio,
                )
                self.assertEqual(
                    composed["earcon_player"]._output is not None,
                    has_fusion_audio,
                )

    async def test_checked_in_mira_config_composes_on_bare_host_without_camera(self):
        _, args, _ = parse_launch_arguments([
            "--config", "config/mira-agentic.toml", "--mode", "run",
        ])
        application = mock.Mock()
        application.run = AsyncMock(return_value=None)
        with (
            patch(
                "embodied_runtime.hardware.fusion_hat.FusionHatSysfs.is_ready",
                new_callable=mock.PropertyMock,
                return_value=False,
            ),
            patch(
                "embodied_runtime.cli.Picamera2CameraBackend.start",
                side_effect=Picamera2DeviceUnavailableError("no camera"),
            ),
            patch("embodied_runtime.cli.build_persistent_memory_store", return_value=None),
            patch("embodied_runtime.cli.build_job_store", return_value=None),
            patch("embodied_runtime.cli.build_job_workspace_store", return_value=None),
            patch("embodied_runtime.cli.RobotApplication", return_value=application) as constructor,
        ):
            self.assertEqual(await _run_application(args, RobotProfile("mira", "Mira")), 0)

        hardware = constructor.call_args.args[1]
        composed = constructor.call_args.kwargs
        self.assertIsInstance(hardware, HostHardwareBackend)
        self.assertIsNone(composed["camera_backend"])
        self.assertIsNone(composed["body_backend"])
        self.assertIsNone(composed["voice_provider"])
        self.assertIsNone(composed["text_to_speech_provider"])
        self.assertIsNone(composed["earcon_player"]._output)
        self.assertIsNotNone(composed["cognition_backend"])
        self.assertTrue(constructor.call_args.args[2].jobs_auto_continue)

    async def test_fusion_and_available_auto_camera_compose_together(self):
        _, args, _ = parse_launch_arguments([
            "--mode", "run", "--hardware", "fusion-hat", "--camera", "auto",
        ])
        camera = mock.Mock()
        camera.identifier = "picamera2"
        camera.is_physical = True
        camera.is_running = False
        application = mock.Mock()
        application.run = AsyncMock(return_value=None)
        with patch(
            "embodied_runtime.cli.Picamera2CameraBackend", return_value=camera,
        ), patch(
            "embodied_runtime.cli.RobotApplication", return_value=application,
        ) as constructor:
            self.assertEqual(await _run_application(args, RobotProfile("mira", "Mira")), 0)
        self.assertIsInstance(
            constructor.call_args.args[1], FusionHatHardwareBackend,
        )
        self.assertIs(constructor.call_args.kwargs["camera_backend"], camera)
        camera.start.assert_called_once_with()
        camera.stop.assert_not_called()

    async def test_auto_camera_is_stopped_if_later_composition_fails(self):
        _, args, _ = parse_launch_arguments([
            "--mode", "run", "--camera", "auto",
        ])
        camera = mock.Mock()
        camera.identifier = "picamera2"
        with patch(
            "embodied_runtime.cli.Picamera2CameraBackend", return_value=camera,
        ), patch(
            "embodied_runtime.cli.build_cognition_backend",
            side_effect=RuntimeError("composition failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "composition failed"):
                await _run_application(args, RobotProfile("mira", "Mira"))
        camera.start.assert_called_once_with()
        camera.stop.assert_called_once_with()


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        # Individual history integration tests supply a temporary root. The
        # pre-existing CLI tests must never write repository-local run data.
        self.history_patch = patch(
            "embodied_runtime.cli.start_run", side_effect=OSError("unavailable")
        )
        self.history_patch.start()

    def tearDown(self) -> None:
        self.history_patch.stop()

    def test_outer_finalization_follows_application_shutdown(self) -> None:
        logs: list[str] = []

        def record(message: str, *args: object) -> None:
            logs.append(message % args if args else message)

        async def stopped_application(*_args: object) -> int:
            record("[APP] stopped")
            return 0

        with patch(
            "embodied_runtime.cli._run_application", side_effect=stopped_application
        ), patch("embodied_runtime.cli.LOGGER.info", side_effect=record):
            self.assertEqual(main(["--diagnostics"]), 0)
        rendered = "\n".join(logs)
        self.assertLess(rendered.index("[APP] stopped"), rendered.index(
            "[PROCESS] application_coroutine status=completed"
        ))
        self.assertLess(rendered.index(
            "[PROCESS] asyncio_cleanup status=completed"
        ), rendered.index("[PROCESS] main status=returning exit_code=0"))

    def test_normal_startup_observability_uses_built_in_pricing(self) -> None:
        async def account_usage(*args: object) -> int:
            observability = args[4]
            observability.provider_completed(
                "openai-responses", "gpt-5.6-luna", "initial",
                input_tokens=1_000, output_tokens=1_000,
            )
            self.assertEqual(
                observability.snapshot()["cost"]["estimated_usd"], "0.001400"
            )
            return 0

        with patch(
            "embodied_runtime.cli._run_application", side_effect=account_usage
        ):
            self.assertEqual(main(["--diagnostics"]), 0)

    def test_runner_cleanup_is_timed_and_invoked_once(self) -> None:
        events: list[str] = []

        class RecordingRunner:
            def run(self, coroutine):
                events.append("run")
                return asyncio.run(coroutine)

            def close(self):
                events.append("close")

        async def application() -> int:
            events.append("application")
            return 7

        with patch("embodied_runtime.cli.asyncio.Runner", RecordingRunner), patch(
            "embodied_runtime.cli.time.perf_counter", side_effect=(10.0, 10.125)
        ), self.assertLogs("embodied_runtime.cli", level="INFO") as captured:
            self.assertEqual(_run_with_asyncio_cleanup(application()), 7)
        self.assertEqual(events, ["run", "application", "close"])
        self.assertIn(
            "[PROCESS] asyncio_cleanup status=completed duration_ms=125.0",
            "\n".join(captured.output),
        )

    def test_keyboard_interrupt_cleans_runner_before_main_returns(self) -> None:
        async def interrupted(*_args: object) -> int:
            raise KeyboardInterrupt

        with patch(
            "embodied_runtime.cli._run_application", side_effect=interrupted
        ), self.assertLogs("embodied_runtime.cli", level="INFO") as captured:
            self.assertEqual(main(["--diagnostics"]), 130)
        logs = "\n".join(captured.output)
        self.assertLess(logs.index(
            "[PROCESS] asyncio_cleanup status=completed"
        ), logs.index("[PROCESS] main status=returning exit_code=130"))

    def test_run_mode_keyboard_interrupt_remains_exit_130(self) -> None:
        with patch(
            "embodied_runtime.cli._run_application", new=AsyncMock(
                side_effect=KeyboardInterrupt
            ),
        ):
            self.assertEqual(main(["--mode", "run"]), 130)

    def test_non_daemon_thread_report_filters_and_sorts(self) -> None:
        class FakeThread:
            def __init__(self, name: str, *, alive: bool, daemon: bool):
                self.name = name
                self._alive = alive
                self.daemon = daemon

            def is_alive(self) -> bool:
                return self._alive

        current = FakeThread("current", alive=True, daemon=False)
        main_thread = FakeThread("main", alive=True, daemon=False)
        threads = (
            current, main_thread,
            FakeThread("z-worker", alive=True, daemon=False),
            FakeThread("daemon", alive=True, daemon=True),
            FakeThread("ended", alive=False, daemon=False),
            FakeThread("a-worker", alive=True, daemon=False),
        )
        self.assertEqual(
            _live_non_daemon_thread_names(  # type: ignore[arg-type]
                threads, current=current, main_thread=main_thread
            ),
            ("a-worker", "z-worker"),
        )

    def test_defaults(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(args.profile, "mira")
        self.assertEqual(args.hardware, "virtual")
        self.assertIsNone(args.startup_prompt)
        self.assertEqual(args.cognition, "none")
        self.assertFalse(args.initiative)
        self.assertFalse(args.initiative_platform_attention)
        self.assertFalse(args.initiative_actions)
        self.assertFalse(args.initiative_messages)
        self.assertFalse(args.initiative_continuation)
        self.assertFalse(args.initiative_goal_closure)

    def test_initiative_requires_cognition_backend(self) -> None:
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            main(["--initiative"])

    def test_platform_attention_requires_explicit_initiative(self) -> None:
        for argv in (
            ["--initiative-platform-attention", "--console"],
            ["--cognition", "openai-responses",
             "--initiative-platform-attention", "--console"],
        ):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit):
                main(argv)

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

    def test_headless_initiative_messages_accept_configured_sms_route(self) -> None:
        argv = ["--mode", "run", "--sms", "--cognition", "openai-responses",
                "--initiative", "--initiative-messages"]
        with patch(
            "embodied_runtime.cli._run_application", new=AsyncMock(return_value=0)
        ):
            self.assertEqual(main(argv), 0)

    def test_headless_initiative_messages_reject_missing_route(self) -> None:
        argv = ["--mode", "run", "--cognition", "openai-responses",
                "--initiative", "--initiative-messages"]
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            main(argv)

    def test_initiative_goal_closure_requires_only_initiative(self) -> None:
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            main(["--cognition", "openai-responses",
                  "--initiative-goal-closure", "--console"])

        argv = ["--cognition", "openai-responses", "--initiative",
                "--initiative-goal-closure", "--console"]
        with patch(
            "embodied_runtime.cli._run_application", new=AsyncMock(return_value=0)
        ) as run:
            self.assertEqual(main(argv), 0)
        args = run.await_args.args[0]
        self.assertTrue(args.initiative_goal_closure)
        self.assertFalse(args.initiative_actions)
        self.assertFalse(args.initiative_messages)

    def test_initiative_continuation_requires_initiative_and_an_extra_effect(self) -> None:
        for argv in (
            ["--initiative-continuation", "--console"],
            ["--cognition", "openai-responses", "--initiative",
             "--initiative-continuation", "--console"],
        ):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit):
                main(argv)
        for permission in ("--initiative-actions", "--initiative-messages"):
            argv = ["--cognition", "openai-responses", "--initiative", permission,
                    "--initiative-continuation", "--console"]
            with self.subTest(permission=permission), patch(
                "embodied_runtime.cli._run_application", new=AsyncMock(return_value=0)
            ):
                self.assertEqual(main(argv), 0)

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

    def test_explicit_host_and_virtual_force_requested_backend(self) -> None:
        self.assertIsInstance(
            build_hardware_backend(build_parser().parse_args(["--hardware", "host"])),
            HostHardwareBackend,
        )
        self.assertIsInstance(
            build_hardware_backend(build_parser().parse_args(["--hardware", "virtual"])),
            VirtualHardwareBackend,
        )

    def test_auto_resolves_fusion_when_driver_is_ready(self) -> None:
        args = build_parser().parse_args(["--hardware", "auto"])
        with patch(
            "embodied_runtime.hardware.fusion_hat.FusionHatSysfs.is_ready",
            new_callable=mock.PropertyMock,
            return_value=True,
        ):
            self.assertIsInstance(build_hardware_backend(args), FusionHatHardwareBackend)

    def test_auto_falls_back_to_physical_host_without_driver(self) -> None:
        args = build_parser().parse_args(["--hardware", "auto"])
        with patch(
            "embodied_runtime.hardware.fusion_hat.FusionHatSysfs.is_ready",
            new_callable=mock.PropertyMock,
            return_value=False,
        ):
            backend = build_hardware_backend(args)
        self.assertIsInstance(backend, HostHardwareBackend)
        backend.start()
        self.assertTrue(backend.is_running)

    def test_servo_test_requires_diagnostics_and_physical_hardware(self) -> None:
        for argv in (["--fusion-servo-test", "P0"],
                     ["--diagnostics", "--fusion-servo-test", "P0"]):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit):
                main(argv)

    def test_battery_test_requires_diagnostics_and_physical_hardware(self) -> None:
        for argv in (["--fusion-battery-test"],
                     ["--diagnostics", "--fusion-battery-test"]):
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
