import builtins
from dataclasses import fields
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from embodied_runtime.app import RobotApplication
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cli import build_camera_backend, build_parser, main
from embodied_runtime.events import EventBus
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile
from embodied_runtime.sensing.camera import CameraBackend, CameraFrame
from embodied_runtime.sensing.camera.picamera2 import (
    Picamera2CameraBackend,
    Picamera2DeviceUnavailableError,
    Picamera2UnavailableError,
)
from embodied_runtime.state import LifecycleState
from tests.test_platform import snapshot

JPEG = b"\xff\xd8test-jpeg\xff\xd9"


class FakePicamera:
    def __init__(self, *, start_error=None):
        self.start_error = start_error
        self.operations = []

    def create_still_configuration(self, **kwargs):
        self.operations.append(("create", kwargs))
        return "still-config"

    def configure(self, configuration):
        self.operations.append(("configure", configuration))

    def start(self):
        self.operations.append(("start",))
        if self.start_error:
            raise self.start_error

    def capture_file(self, output, *, format):
        self.operations.append(("capture", format))
        output.write(JPEG)

    def stop(self):
        self.operations.append(("stop",))

    def close(self):
        self.operations.append(("close",))


class Picamera2BackendTests(unittest.TestCase):
    def test_importing_camera_abstraction_does_not_import_picamera2(self):
        script = """
import builtins
import sys

real_import = builtins.__import__

def reject_picamera2(name, *args, **kwargs):
    if name == "picamera2" or name.startswith("picamera2."):
        raise AssertionError("camera abstraction imported picamera2")
    return real_import(name, *args, **kwargs)

builtins.__import__ = reject_picamera2
import embodied_runtime.sensing.camera
import embodied_runtime.sensing.camera.base
assert "picamera2" not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lifecycle_and_one_shot_jpeg_metadata(self):
        camera = FakePicamera()
        backend = Picamera2CameraBackend(lambda: camera, clock_ns=lambda: 123)
        self.assertIsInstance(backend, CameraBackend)
        self.assertEqual(backend.identifier, "picamera2")
        self.assertTrue(backend.is_physical)
        self.assertFalse(backend.is_running)
        backend.start()
        self.assertTrue(backend.is_running)
        frame = backend.capture_frame()
        self.assertEqual(
            frame, CameraFrame(JPEG, "image/jpeg", 640, 480, 123)
        )
        with self.assertRaises(Exception):
            frame.width = 1  # type: ignore[misc]
        backend.stop()
        backend.stop()
        self.assertFalse(backend.is_running)
        self.assertEqual(camera.operations[-2:], [("stop",), ("close",)])

    def test_capture_requires_running(self):
        with self.assertRaisesRegex(RuntimeError, "running"):
            Picamera2CameraBackend(lambda: FakePicamera()).capture_frame()

    def test_missing_package_has_dedicated_operator_error(self):
        real_import = builtins.__import__

        def missing(name, *args, **kwargs):
            if name == "picamera2":
                raise ModuleNotFoundError(name)
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=missing):
            with self.assertRaisesRegex(Picamera2UnavailableError, "system site"):
                Picamera2CameraBackend().start()

    def test_device_failure_closes_partial_camera(self):
        camera = FakePicamera(start_error=RuntimeError("no cameras available"))
        backend = Picamera2CameraBackend(lambda: camera)
        with self.assertRaisesRegex(
            Picamera2DeviceUnavailableError, "no usable CSI camera"
        ):
            backend.start()
        self.assertFalse(backend.is_running)
        self.assertEqual(camera.operations[-1], ("close",))
        backend.stop()

    def test_public_frame_has_no_vendor_types(self):
        self.assertEqual(
            [field.name for field in fields(CameraFrame)],
            ["data", "media_type", "width", "height", "captured_at_ns"],
        )


class PlatformProvider:
    def snapshot(self):
        return snapshot()


class FakeCamera(CameraBackend):
    identifier = "fake"
    is_physical = False

    def __init__(self, *, fail_start=False):
        self._running = False
        self.fail_start = fail_start
        self.starts = 0
        self.stops = 0
        self.captures = 0
        self.frame = CameraFrame(JPEG, "image/jpeg", 2, 1, 99)

    @property
    def is_running(self):
        return self._running

    def start(self):
        self.starts += 1
        if self.fail_start:
            self._running = True
            raise RuntimeError("camera failed")
        self._running = True

    def stop(self):
        self.stops += 1
        self._running = False

    def capture_frame(self):
        self.captures += 1
        return self.frame


class ApplicationCameraTests(unittest.IsolatedAsyncioTestCase):
    def application(self, camera=None, events=None, body_backend=None):
        return RobotApplication(
            RobotProfile("test", "Test"),
            VirtualHardwareBackend(),
            events=events,
            platform_provider=PlatformProvider(),
            body_backend=body_backend,
            camera_backend=camera,
        )

    async def test_application_owns_camera_and_capture_is_transient(self):
        camera = FakeCamera()
        events = EventBus()
        published = []
        original_publish = events.publish

        async def record(event):
            published.append(event)
            await original_publish(event)

        events.publish = record
        app = self.application(camera, events)
        created_state = app.runtime_state
        with self.assertRaisesRegex(RuntimeError, "running application"):
            app.capture_camera_frame()
        await app.start()
        self.assertTrue(camera.is_running)
        state_before = app.runtime_state
        event_count = len(published)
        frame = app.capture_camera_frame()
        self.assertIs(frame, camera.frame)
        self.assertEqual(camera.captures, 1)
        self.assertIs(app.runtime_state, state_before)
        self.assertNotIn("data", [field.name for field in fields(app.runtime_state)])
        self.assertEqual(len(published), event_count)
        await app.stop()
        self.assertFalse(camera.is_running)
        self.assertEqual(camera.stops, 1)
        self.assertEqual(created_state.lifecycle, LifecycleState.CREATED)

    async def test_capture_requires_configured_camera(self):
        app = self.application()
        await app.start()
        with self.assertRaisesRegex(RuntimeError, "No camera"):
            app.capture_camera_frame()
        await app.stop()

    async def test_camera_summary_is_transient_resource_metadata(self):
        camera = FakeCamera()
        app = self.application(camera)
        state_before = app.runtime_state
        summary = app.camera_summary()
        self.assertEqual(summary.backend, "fake")
        self.assertFalse(summary.is_physical)
        self.assertFalse(summary.is_running)
        self.assertIs(app.runtime_state, state_before)

        await app.start()
        running_state = app.runtime_state
        self.assertTrue(app.camera_summary().is_running)
        self.assertIs(app.runtime_state, running_state)
        await app.stop()

    def test_camera_summary_is_none_without_configured_camera(self):
        app = self.application()
        state_before = app.runtime_state
        self.assertIsNone(app.camera_summary())
        self.assertIs(app.runtime_state, state_before)

    async def test_failed_camera_start_unwinds_started_resources(self):
        camera = FakeCamera(fail_start=True)
        events = EventBus()
        app = self.application(camera, events)
        with self.assertRaisesRegex(RuntimeError, "camera failed"):
            await app.start()
        self.assertEqual(app.state, LifecycleState.STOPPED)
        self.assertFalse(app.hardware.is_running)
        self.assertFalse(events.is_running)
        self.assertFalse(camera.is_running)
        self.assertEqual(camera.stops, 1)

    async def test_body_start_failure_does_not_stop_unattempted_camera(self):
        camera = FakeCamera()
        body = VirtualBodyBackend()
        with patch.object(body, "start", side_effect=RuntimeError("body failed")):
            app = self.application(camera, body_backend=body)
            with self.assertRaisesRegex(RuntimeError, "body failed"):
                await app.start()
        self.assertEqual(app.state, LifecycleState.STOPPED)
        self.assertEqual(camera.starts, 0)
        self.assertEqual(camera.stops, 0)


class CameraCliTests(unittest.TestCase):
    def test_default_is_none_and_physical_selection_is_explicit(self):
        args = build_parser().parse_args([])
        self.assertEqual(args.camera, "none")
        self.assertIsNone(args.camera_test)
        selected = build_parser().parse_args(["--camera", "picamera2"])
        self.assertIsInstance(build_camera_backend(selected), Picamera2CameraBackend)

    def test_camera_test_validation(self):
        for argv in (["--camera", "picamera2", "--camera-test", "out.jpg"],
                     ["--diagnostics", "--camera-test", "out.jpg"]):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit):
                main(argv)

    def test_diagnostic_writes_exactly_one_returned_frame(self):
        camera = FakeCamera()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "frame.jpg"
            with patch("embodied_runtime.cli.build_camera_backend", return_value=camera):
                with patch("builtins.print") as printer:
                    result = main([
                        "--camera", "picamera2", "--diagnostics",
                        "--camera-test", str(output),
                    ])
            self.assertEqual(result, 0)
            self.assertEqual(output.read_bytes(), JPEG)
            self.assertEqual(camera.captures, 1)
            rendered = " ".join(str(call.args[0]) for call in printer.call_args_list)
            self.assertIn("backend=fake", rendered)
            self.assertIn(f"output={output}", rendered)

    def test_unavailable_camera_is_concise_and_nonzero(self):
        error = Picamera2UnavailableError("Picamera2 missing")
        with patch.object(Picamera2CameraBackend, "start", side_effect=error):
            with patch("sys.stderr") as stderr:
                result = main(["--camera", "picamera2", "--diagnostics"])
        self.assertEqual(result, 2)
        rendered = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("Picamera2 missing", rendered)
