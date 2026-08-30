import unittest
from unittest.mock import patch

from embodied_runtime.app import ApplicationOptions, LifecycleState, RobotApplication
from embodied_runtime.cli import build_parser, format_summary
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


class ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hardware = VirtualHardwareBackend()
        self.application = RobotApplication(
            RobotProfile("test", "Test Robot"),
            self.hardware,
            ApplicationOptions(startup_prompt="private prompt"),
        )

    def test_start_and_stop(self) -> None:
        self.assertEqual(self.application.state, LifecycleState.CREATED)
        self.application.start()
        self.assertEqual(self.application.state, LifecycleState.RUNNING)
        self.assertTrue(self.hardware.is_running)
        self.application.stop()
        self.assertEqual(self.application.state, LifecycleState.STOPPED)
        self.assertFalse(self.hardware.is_running)

    def test_diagnostics_summary(self) -> None:
        self.application.start()
        summary = self.application.summary()
        rendered = format_summary(summary)
        self.assertEqual(summary.lifecycle_status, LifecycleState.RUNNING)
        self.assertTrue(summary.startup_prompt_provided)
        self.assertEqual(
            rendered,
            "[DIAG] profile=test name='Test Robot' hardware=virtual physical=false "
            "capabilities=none startup_prompt_provided=true lifecycle=running",
        )
        self.assertNotIn("\n", rendered)
        self.assertNotIn("private prompt", rendered)
        self.application.stop()

    def test_keyboard_interrupt_logs_and_stops(self) -> None:
        with patch.object(self.application._stop_requested, "wait", side_effect=KeyboardInterrupt):
            with self.assertLogs("embodied_runtime.app", level="INFO") as logs:
                self.application.run()

        self.assertTrue(any(message.endswith("[APP] interrupted") for message in logs.output))
        self.assertEqual(self.application.state, LifecycleState.STOPPED)
        self.assertFalse(self.hardware.is_running)


class CliTests(unittest.TestCase):
    def test_defaults(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(args.profile, "mira")
        self.assertEqual(args.hardware, "virtual")
        self.assertIsNone(args.startup_prompt)

    def test_optional_startup_prompt(self) -> None:
        args = build_parser().parse_args(["Good morning, Mira."])
        self.assertEqual(args.startup_prompt, "Good morning, Mira.")
