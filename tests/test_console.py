import asyncio
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cli import build_parser, build_platform_monitor_policy
from embodied_runtime.cognition import CognitionError
from embodied_runtime.console import AsyncLineTerminal, RuntimeConsole, run_console_session
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile
from embodied_runtime.sensing.camera import CameraBackend, CameraFrame
from tests.test_platform import snapshot


class CountingProvider:
    def __init__(self, samples):
        self.samples = iter(samples)
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return next(self.samples)


class FakeTerminal:
    def __init__(self, lines):
        self.lines = iter(lines)
        self.output = ""

    def write(self, text):
        self.output += text

    async def read_line(self, prompt):
        self.write(prompt)
        return next(self.lines)


class TtyStringIO(io.StringIO):
    def __init__(self, is_tty=True):
        super().__init__()
        self._is_tty = is_tty

    def isatty(self):
        return self._is_tty


JPEG = b"\xff\xd8console-jpeg\xff\xd9"


class FakeCamera(CameraBackend):
    identifier = "fake-camera"
    is_physical = True

    def __init__(self):
        self.running = False
        self.captures = 0
        self.capture_error = None

    @property
    def is_running(self):
        return self.running

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def capture_frame(self):
        self.captures += 1
        if self.capture_error is not None:
            raise self.capture_error
        return CameraFrame(JPEG, "image/jpeg", 640, 480, 1)


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.first = snapshot(
            hostname="old-host", model="Test Board", uptime_seconds=93784,
            load_averages=(0.1, 0.2, 0.3),
            memory_total_bytes=415 * 1024 * 1024,
            memory_available_bytes=260 * 1024 * 1024,
            cpu_temperature_celsius=39.24, captured_monotonic=10,
        )
        self.provider = CountingProvider([self.first])
        self.app = RobotApplication(
            RobotProfile("test", "Test Robot"), VirtualHardwareBackend(),
            ApplicationOptions(startup_prompt="secret words"),
            platform_provider=self.provider,
            body_backend=VirtualBodyBackend(),
        )
        await self.app.start()
        self.console = RuntimeConsole(self.app, monotonic=lambda: 11.75)

    async def asyncTearDown(self):
        await self.app.stop()

    def test_prompt_and_heading_derive_from_profile(self):
        self.assertEqual(self.console.prompt, "test> ")
        self.assertEqual(self.console.heading, "Test Robot Runtime Console")

    def test_help_is_exact_and_alias_matches(self):
        expected = (
            "Commands\n"
            "  status                         Show current runtime overview\n"
            "  platform                       Show current host platform state\n"
            "  hardware                       Show robot hardware backend\n"
            "  body                           Show current body state\n"
            "  body orient <yaw> <pitch>      Set semantic body orientation\n"
            "  camera status                  Show configured camera resource\n"
            "  camera capture <output_path>   Capture one JPEG to an explicit path\n"
            "  presence                       Show current presence state\n"
            "  simulate presence <on|off>     Inject virtual presence\n"
            "  ask <message>                  Send one text cognition request\n"
            "  memory                         Show working-memory metadata\n"
            "  memory clear                   Clear session working memory\n"
            "  goal                           Show current active goal\n"
            "  goal clear                     Clear current active goal\n"
            "  attention                      Show initiative attention state\n"
            "  help                           Show this help\n"
            "  quit                           Stop the console and runtime\n"
            "  exit                           Stop the console and runtime"
        )
        self.assertEqual(self.console.execute("help"), (expected, False))
        self.assertEqual(self.console.execute("?"), (expected, False))

    def test_memory_metadata_and_clear_leave_runtime_state_untouched(self):
        self.app.working_memory.append("one", "answer")
        self.app.working_memory.append("two", "answer")
        state = self.app.runtime_state
        report, stop = self.console.execute("memory")
        self.assertFalse(stop)
        self.assertEqual(report, (
            "Working memory\n"
            "  turns:         2\n"
            "  capacity:      6\n"
            "  text_limit:    2000"
        ))
        self.assertEqual(self.console.execute("memory clear"), (
            "Working memory\n  cleared:       2\n  turns:         0", False
        ))
        self.assertIs(self.app.runtime_state, state)
        self.assertEqual(self.app.working_memory.snapshot(), ())

    def test_goal_show_and_clear_preserve_state_and_memory(self):
        state = self.app.runtime_state
        self.app.working_memory.append("old", "history")
        memory = self.app.working_memory.snapshot()
        self.assertEqual(self.console.execute("goal"), (
            "Active goal\n  state:         none", False
        ))
        self.app.set_goal("keep watching")
        self.assertEqual(self.console.execute("goal"), (
            "Active goal\n  state:         active\n  description:   keep watching", False
        ))
        self.assertEqual(self.console.execute("goal clear"), (
            "Active goal\n  cleared:       true", False
        ))
        self.assertIsNone(self.app.active_goal)
        self.assertIs(self.app.runtime_state, state)
        self.assertEqual(self.app.working_memory.snapshot(), memory)
        self.assertTrue(self.app.events.is_running)

    def test_attention_diagnostics_are_explicit_and_not_in_status(self):
        self.assertEqual(self.console.execute("attention"), (
            "Attention\n"
            "  enabled:       false\n"
            "  state:         disabled\n"
            "  last_trigger:  none\n"
            "  last_source:   none\n"
            "  last_action:   none\n"
            "  last_action_status: none\n"
            "  last_response: unavailable\n"
            "  last_inspection_state: not_run\n"
            "  last_inspection_area: none\n"
            "  last_inspection_status: none\n"
            "  last_continuation_state: not_run\n"
            "  last_continuation_action: none\n"
            "  last_continuation_status: none\n"
            "  last_continuation_response: unavailable\n"
            "  last_outcome_state: not_run\n"
            "  last_goal_closure: none\n"
            "  last_outcome_response: unavailable", False
        ))
        self.assertNotIn("Attention", self.console.execute("status")[0])

    async def test_ask_uses_raw_payload_and_displays_response(self):
        calls = []

        async def request(message):
            calls.append(message)
            return "cognition online"

        self.app.request_cognition = request  # type: ignore[method-assign]
        report, stop = await self.console.execute_async("ask What's  happening today?")
        self.assertEqual(calls, ["What's  happening today?"])
        self.assertEqual(report, "Test Robot: cognition online")
        self.assertFalse(stop)

    async def test_ask_errors_are_nonfatal_and_session_remains_usable(self):
        self.assertEqual(
            await self.console.execute_async("ask"), ("Usage: ask <message>.", False)
        )
        for error in (
            RuntimeError("No cognition backend is configured"),
            CognitionError("provider unavailable"),
        ):
            async def fail(_message, error=error):
                raise error

            self.app.request_cognition = fail  # type: ignore[method-assign]
            report, stop = await self.console.execute_async("ask hello")
            self.assertIn(str(error), report)
            self.assertFalse(stop)
            self.assertIn("Platform", self.console.execute("platform")[0])

    def test_ask_requires_async_session(self):
        report, stop = self.console.execute("ask What's happening?")
        self.assertIn("active asynchronous console session", report)
        self.assertFalse(stop)

    def test_camera_status_without_configured_camera(self):
        self.assertEqual(
            self.console.execute("camera status"),
            ("Camera\n  state:         unavailable", False),
        )

    async def test_camera_status_and_capture_use_application_api(self):
        camera = FakeCamera()
        app = RobotApplication(
            RobotProfile("camera", "Camera Robot"),
            VirtualHardwareBackend(),
            platform_provider=CountingProvider([self.first]),
            camera_backend=camera,
        )
        await app.start()
        console = RuntimeConsole(app)
        state_before = app.runtime_state
        published = []
        original_publish = app.events.publish

        async def record(event):
            published.append(event)
            await original_publish(event)

        app.events.publish = record
        self.assertEqual(
            console.execute("camera status"),
            (
                "Camera\n"
                "  backend:       fake-camera\n"
                "  physical:      true\n"
                "  running:       true",
                False,
            ),
        )

        calls = 0
        capture = app.capture_camera_frame

        def recording_capture():
            nonlocal calls
            calls += 1
            return capture()

        app.capture_camera_frame = recording_capture  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "console.jpg"
            report, stop = await console.execute_async(f"camera capture {output}")
            self.assertEqual(output.read_bytes(), JPEG)
        self.assertFalse(stop)
        self.assertEqual(calls, 1)
        self.assertEqual(camera.captures, 1)
        self.assertIn("backend:       fake-camera", report)
        self.assertIn("width:         640", report)
        self.assertIn("height:        480", report)
        self.assertIn("media_type:    image/jpeg", report)
        self.assertIn(f"bytes:         {len(JPEG)}", report)
        self.assertIn(f"output:        {output}", report)
        self.assertIn("status:        ok", report)
        self.assertIs(app.runtime_state, state_before)
        self.assertEqual(published, [])
        await app.stop()

    async def test_camera_capture_validation_and_errors_keep_session_alive(self):
        for command in ("camera capture", "camera capture one two"):
            report, stop = await self.console.execute_async(command)
            self.assertEqual(report, "Usage: camera capture <output_path>.")
            self.assertFalse(stop)

        report, stop = await self.console.execute_async("camera capture nowhere.jpg")
        self.assertIn("No camera backend is configured", report)
        self.assertFalse(stop)

        camera = FakeCamera()
        app = RobotApplication(
            RobotProfile("camera", "Camera Robot"),
            VirtualHardwareBackend(),
            platform_provider=CountingProvider([self.first]),
            camera_backend=camera,
        )
        await app.start()
        console = RuntimeConsole(app)
        camera.capture_error = RuntimeError("sensor unavailable")
        report, stop = await console.execute_async("camera capture ignored.jpg")
        self.assertIn("Camera capture failed: sensor unavailable", report)
        self.assertFalse(stop)

        camera.capture_error = None
        with patch.object(Path, "write_bytes", side_effect=OSError("read-only path")):
            report, stop = await console.execute_async("camera capture /bad/output.jpg")
        self.assertIn("Camera capture failed: read-only path", report)
        self.assertFalse(stop)
        await app.stop()

    def test_status_projects_current_state_without_sampling(self):
        calls = self.provider.calls
        report, stop = self.console.execute("status")
        self.assertFalse(stop)
        self.assertIn("profile:       test", report)
        self.assertIn("name:          Test Robot", report)
        self.assertIn("lifecycle:     running", report)
        self.assertIn("hostname:      old-host", report)
        self.assertIn("state_age_s:   1.8", report)
        self.assertIn("uptime:        1d 02:03:04", report)
        self.assertIn("cpu_temp_c:    39.2", report)
        self.assertIn("memory:        260 / 415 MiB available (62.7%)", report)
        self.assertIn("backend:       virtual", report)
        self.assertIn("physical:      false", report)
        self.assertIn("capabilities:  none", report)
        self.assertEqual(self.provider.calls, calls)
        self.assertNotIn("secret words", report)

    def test_all_reports_read_new_authoritative_snapshot_without_sampling(self):
        newer = snapshot(hostname="new-host", cpu_temperature_celsius=41)
        self.app._runtime_state = replace(self.app.runtime_state, platform=newer)
        calls = self.provider.calls
        self.assertIn("new-host", self.console.execute("platform")[0])
        self.assertIn("new-host", self.console.execute("status")[0])
        hardware = self.console.execute("hardware")[0]
        self.assertNotIn("hostname", hardware)
        self.assertEqual(self.provider.calls, calls)

    def test_missing_platform_and_metrics_are_clear(self):
        self.app._runtime_state = replace(self.app.runtime_state, platform=None)
        self.assertEqual(
            self.console.execute("platform")[0],
            "Platform\n  state:         unavailable",
        )
        self.app._runtime_state = replace(self.app.runtime_state, platform=snapshot())
        report = self.console.execute("platform")[0]
        self.assertIn("cpu_temp_c:    unknown", report)
        self.assertIn("memory:        unknown", report)
        self.assertIn("uptime:        unknown", report)

    def test_empty_unknown_and_exit_commands(self):
        self.assertEqual(self.console.execute("  "), ("", False))
        self.assertEqual(self.console.execute("quit"), ("", True))
        self.assertEqual(self.console.execute("exit"), ("", True))
        self.assertEqual(
            self.console.execute("events"),
            ("Unknown command: events. Type 'help' for commands.", False),
        )

    async def test_body_and_presence_commands_use_application_semantic_apis(self):
        orientation_calls = []
        presence_calls = []
        set_orientation = self.app.set_body_orientation
        observe_presence = self.app.observe_presence

        async def recording_orientation(**arguments):
            orientation_calls.append(arguments)
            return await set_orientation(**arguments)

        async def recording_presence(**arguments):
            presence_calls.append(arguments)
            return await observe_presence(**arguments)

        self.app.set_body_orientation = recording_orientation  # type: ignore[method-assign]
        self.app.observe_presence = recording_presence  # type: ignore[method-assign]

        report, stop = await self.console.execute_async("BoDy OrIeNt 30 -10")
        self.assertFalse(stop)
        self.assertIn("yaw_deg:       30.0", report)
        self.assertEqual(
            orientation_calls,
            [{"yaw_degrees": 30.0, "pitch_degrees": -10.0, "source": "console"}],
        )

        report, stop = await self.console.execute_async("simulate presence ON")
        self.assertFalse(stop)
        self.assertIn("status:        present", report)
        self.assertEqual(
            presence_calls,
            [{"present": True, "source": "virtual_scenario"}],
        )
        self.assertIn("yaw_deg:       30.0", self.console.execute("body")[0])
        self.assertIn(
            "source:        virtual_scenario",
            self.console.execute("presence")[0],
        )

    async def test_body_orientation_error_does_not_mutate_state(self):
        previous = self.app.runtime_state.body
        report, stop = await self.console.execute_async("body orient 181 0")
        self.assertFalse(stop)
        self.assertIn("Invalid body orientation", report)
        self.assertIs(self.app.runtime_state.body, previous)

    def test_malformed_quoted_command_is_concise(self):
        report, stop = self.console.execute("body '")
        self.assertFalse(stop)
        self.assertIn("Unable to parse command", report)

    async def test_session_eof_and_quit_terminate(self):
        for lines in ([None], ["", "quit"]):
            terminal = FakeTerminal(lines)
            await run_console_session(self.console, terminal)  # type: ignore[arg-type]
            self.assertIn("Test Robot Runtime Console", terminal.output)
            self.assertNotIn("secret words", terminal.output)

    async def test_monitor_can_update_while_console_waits(self):
        waiting = asyncio.Event()
        release = asyncio.Event()

        class WaitingTerminal(FakeTerminal):
            async def read_line(inner_self, prompt):
                inner_self.write(prompt)
                waiting.set()
                await release.wait()
                return None

        session = asyncio.create_task(
            run_console_session(self.console, WaitingTerminal([]))  # type: ignore[arg-type]
        )
        await waiting.wait()
        newer = snapshot(hostname="updated")
        self.app._replace_platform_state(newer)
        self.assertTrue(self.app._platform_monitor.is_running)
        self.assertIn("updated", self.console.execute("status")[0])
        release.set()
        await session

    async def test_session_cancellation_leaves_no_task(self):
        terminal = FakeTerminal([])

        async def blocked(_prompt):
            await asyncio.Event().wait()

        terminal.read_line = blocked
        task = asyncio.create_task(run_console_session(self.console, terminal))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(task.done())


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    def test_colour_detection_and_explicit_override(self):
        tty = TtyStringIO()
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(AsyncLineTerminal(stdout=tty).style.enabled)
            self.assertFalse(AsyncLineTerminal(stdout=tty, no_color=True).style.enabled)
            self.assertFalse(AsyncLineTerminal(stdout=io.StringIO()).style.enabled)
        with patch.dict("os.environ", {"NO_COLOR": ""}):
            self.assertFalse(AsyncLineTerminal(stdout=tty).style.enabled)

    async def test_pipe_eof_is_nonblocking_and_cancellable(self):
        read_fd, write_fd = __import__("os").pipe()
        reader = __import__("os").fdopen(read_fd)
        writer = __import__("os").fdopen(write_fd, "w")
        output = io.StringIO()
        terminal = AsyncLineTerminal(reader, output)
        writer.write("hello\n")
        writer.flush()
        self.assertEqual(await terminal.read_line("test> "), "hello")
        writer.close()
        self.assertIsNone(await terminal.read_line("test> "))
        reader.close()


class ConsoleCliTests(unittest.TestCase):
    def test_console_flag_and_defaults(self):
        defaults = build_parser().parse_args([])
        self.assertFalse(defaults.console)
        self.assertFalse(defaults.diagnostics)
        self.assertTrue(build_parser().parse_args(["--console"]).console)
        self.assertEqual(defaults.cognition, "none")
        self.assertFalse(defaults.initiative)
        self.assertFalse(defaults.initiative_actions)
        self.assertFalse(defaults.initiative_continuation)
        self.assertFalse(defaults.initiative_goal_closure)
        self.assertFalse(defaults.no_color)
        self.assertTrue(build_parser().parse_args(["--no-color"]).no_color)
        self.assertEqual(
            build_parser().parse_args(["--cognition", "openai-responses"]).cognition,
            "openai-responses",
        )
        enabled = build_parser().parse_args([
            "--cognition", "openai-responses", "--initiative",
        ])
        self.assertTrue(enabled.initiative)

    def test_console_disables_only_monitor_heartbeat_logging(self):
        self.assertIsNone(build_platform_monitor_policy(build_parser().parse_args([])))
        policy = build_platform_monitor_policy(
            build_parser().parse_args(["--console"])
        )
        self.assertIsNotNone(policy)
        self.assertIsNone(policy.heartbeat_interval_seconds)
        self.assertEqual(policy.interval_seconds, 5.0)

    def test_console_and_diagnostics_are_exclusive(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--console", "--diagnostics"])
