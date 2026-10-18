import builtins
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from embodied_runtime.app import (
    CAMERA_CAPTURE_OWNER, CAMERA_RESOURCE, RobotApplication,
)
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cli import build_camera_backend, build_parser, main
from embodied_runtime.events import EventBus
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile
from embodied_runtime.resources import (
    ResourceArbiter, ResourceBusyError, ResourceOwner,
)
from embodied_runtime.sensing.camera import CameraBackend, CameraFrame
from embodied_runtime.sensing.camera.picamera2 import (
    Picamera2CameraBackend,
    Picamera2DeviceUnavailableError,
    Picamera2UnavailableError,
)
from embodied_runtime.state import LifecycleState
from tests.test_platform import snapshot

JPEG = b"\xff\xd8test-jpeg\xff\xd9"
CAMERA_LOGGER = "embodied_runtime.sensing.camera.picamera2"


def sensor_modes(entries, depths=(10,)):
    # Picamera2 crop_limits use full sensor coordinates, not readout coordinates.
    return [
        {"size": size, "bit_depth": depth, "crop_limits": crop,
         "format": f"SBGGR{depth}_CSI2P", "unpacked": f"SBGGR{depth}",
         "fps": fps, "exposure_limits": (100, 1000000, None)}
        for depth in depths for size, crop, fps in entries
    ]


IMX219_MODES = sensor_modes([
    ((640, 480), (1000, 752, 1280, 960), 103.33),
    ((1640, 1232), (0, 0, 3280, 2464), 41.85),
    ((1920, 1080), (680, 692, 1920, 1080), 47.57),
    ((3280, 2464), (0, 0, 3280, 2464), 21.19),
], depths=(8, 10))
OV5647_MODES = sensor_modes([
    ((640, 480), (16, 0, 2560, 1920), 58.92),
    ((1296, 972), (0, 0, 2592, 1944), 43.25),
    ((1920, 1080), (348, 434, 1928, 1080), 30.62),
    ((2592, 1944), (0, 0, 2592, 1944), 15.63),
])


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


class ModePicamera(FakePicamera):
    def __init__(self, modes=IMX219_MODES, *, automatic_size=(640, 480), **kwargs):
        super().__init__(**kwargs)
        self.sensor_modes = deepcopy(modes)
        self.automatic_size = automatic_size
        self.effective = {}
        self.camera_controls = {}
        self.ignore_sensor = False
        self.crop_override = None
        self.capture_crop = None
        self.capture_error = None
        self.configure_error = None

    def create_still_configuration(self, *, main, sensor=None):
        kwargs = {"main": main}
        if sensor is not None:
            kwargs["sensor"] = sensor
        self.operations.append(("create", deepcopy(kwargs)))
        return kwargs

    def configure(self, configuration):
        super().configure(deepcopy(configuration))
        if self.configure_error:
            raise self.configure_error
        sensor = configuration.get("sensor") if not self.ignore_sensor else None
        sensor = sensor or {"output_size": self.automatic_size, "bit_depth": 10}
        self.effective = {"main": configuration["main"], "sensor": sensor}
        modes = self.sensor_modes if isinstance(self.sensor_modes, list) else []
        mode = next((m for m in modes if isinstance(m, dict)
                     and m.get("size") == sensor["output_size"]
                     and m.get("bit_depth") == sensor["bit_depth"]), None)
        if mode and mode.get("crop_limits"):
            crop = self.crop_override or mode["crop_limits"]
            self.camera_controls = {"ScalerCrop": ((0, 0, 1, 1), crop, crop)}

    def camera_configuration(self):
        return deepcopy(self.effective)

    def capture_file(self, output, *, format):
        if self.capture_error:
            raise self.capture_error
        super().capture_file(output, format=format)
        return {"ScalerCrop": self.capture_crop or self.camera_controls["ScalerCrop"][2]}


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

    def test_imx219_uses_binned_full_view_instead_of_cropped_vga_or_full_resolution(self):
        camera = ModePicamera()
        backend = Picamera2CameraBackend(lambda: camera, clock_ns=lambda: 123)
        with self.assertLogs(CAMERA_LOGGER, level="INFO") as logs:
            backend.start()
        self.assertEqual(camera.effective["sensor"], {
            "output_size": (1640, 1232), "bit_depth": 10,
        })
        self.assertEqual(camera.effective["main"], {"size": (640, 480)})
        self.assertIn("automatic_fallback=False", logs.output[0])
        self.assertIn("crop_limits=(0, 0, 3280, 2464)", logs.output[0])
        self.assertEqual(backend.capture_frame(), CameraFrame(JPEG, "image/jpeg", 640, 480, 123))
        backend.start()  # Already running: no more probing/configuration.
        self.assertEqual(sum(op[0] == "start" for op in camera.operations), 1)
        self.assertEqual(sum(op[0] == "capture" for op in camera.operations), 1)
        backend.stop()
        self.assertEqual(camera.operations[-2:], [("stop",), ("close",)])

    def test_ov5647_keeps_economical_automatic_vga_with_near_full_coverage(self):
        camera = ModePicamera(OV5647_MODES)
        with self.assertLogs(CAMERA_LOGGER, level="INFO") as logs:
            Picamera2CameraBackend(lambda: camera).start()
        self.assertEqual(camera.effective["sensor"]["output_size"], (640, 480))
        self.assertEqual([op for op in camera.operations if op[0] == "create"], [
            ("create", {"main": {"size": (640, 480)}}),
        ])
        self.assertIn("automatic_coverage_sufficient", logs.output[0])
        self.assertIn("automatic_fallback=False", logs.output[0])

    def test_full_resolution_automatic_mode_is_replaced_with_cheaper_equivalent(self):
        camera = ModePicamera(automatic_size=(3280, 2464))
        Picamera2CameraBackend(lambda: camera).start()
        self.assertEqual(camera.effective["sensor"]["output_size"], (1640, 1232))

    def test_selection_does_not_depend_on_reported_mode_order(self):
        camera = ModePicamera(list(reversed(IMX219_MODES)))
        Picamera2CameraBackend(lambda: camera).start()
        self.assertEqual(camera.effective["sensor"]["output_size"], (1640, 1232))

    def test_output_aspect_ratio_is_used_when_comparing_coverage(self):
        # For a wide output, a wide crop can be as useful as the full sensor.
        camera = ModePicamera(sensor_modes([
            ((1920, 1080), (0, 309, 3280, 1845), 40),
            ((3280, 2464), (0, 0, 3280, 2464), 20),
        ]), automatic_size=(3280, 2464))
        Picamera2CameraBackend(lambda: camera, size=(640, 360)).start()
        self.assertEqual(camera.effective["sensor"]["output_size"], (1920, 1080))
        self.assertEqual(camera.effective["main"]["size"], (640, 360))

    def test_unavailable_or_incomplete_metadata_retains_automatic_configuration(self):
        incomplete = deepcopy(IMX219_MODES)
        incomplete[0].pop("crop_limits")
        for modes in (None, [], [{"format": "MJPEG"}], incomplete,
                      [{"size": (1640, 1232), "bit_depth": 10, "crop_limits": (0, 0, 0, 2464)}]):
            with self.subTest(modes=modes):
                camera = ModePicamera(modes)
                with self.assertLogs(CAMERA_LOGGER, level="INFO") as logs:
                    Picamera2CameraBackend(lambda: camera).start()
                self.assertEqual(sum(op[0] == "configure" for op in camera.operations), 1)
                self.assertIn("automatic_fallback=True", logs.output[0])

    def test_absent_api_uses_original_configuration_call(self):
        camera = FakePicamera()
        with self.assertLogs(CAMERA_LOGGER, level="INFO") as logs:
            Picamera2CameraBackend(lambda: camera).start()
        self.assertEqual(camera.operations[:2], [
            ("create", {"main": {"size": (640, 480)}}),
            ("configure", "still-config"),
        ])
        self.assertIn("automatic_fallback=True", logs.output[0])

    def test_unsupported_mode_enumeration_falls_back_but_io_failures_propagate(self):
        for error, fallback in ((NotImplementedError("old API"), True),
                                (KeyError("ScalerCrop"), True),
                                (RuntimeError("device disconnected"), False)):
            with self.subTest(error=error):
                class ProbeCamera(FakePicamera):
                    @property
                    def sensor_modes(self):
                        raise error

                camera = ProbeCamera()
                backend = Picamera2CameraBackend(lambda: camera)
                if fallback:
                    backend.start()
                    self.assertTrue(backend.is_running)
                else:
                    with self.assertRaisesRegex(Picamera2DeviceUnavailableError, "disconnected"):
                        backend.start()
                    self.assertEqual(camera.operations, [("close",)])

    def test_legacy_configuration_without_sensor_field_keeps_automatic(self):
        class LegacyCamera(ModePicamera):
            def camera_configuration(self):
                return {"main": self.effective["main"]}

        camera = LegacyCamera()
        with self.assertLogs(CAMERA_LOGGER, level="INFO") as logs:
            Picamera2CameraBackend(lambda: camera).start()
        self.assertEqual(sum(op[0] == "configure" for op in camera.operations), 1)
        self.assertIn("sensor_configuration_unsupported", logs.output[0])

    def test_incomplete_effective_sensor_metadata_keeps_automatic(self):
        camera = ModePicamera()
        with patch.object(camera, "camera_configuration", return_value={
            "main": {"size": (640, 480)}, "sensor": {"output_size": (640, 480)},
        }):
            Picamera2CameraBackend(lambda: camera).start()
        self.assertEqual(sum(op[0] == "configure" for op in camera.operations), 1)

    def test_camera_swap_after_stop_reprobes_modes(self):
        imx219, ov5647 = ModePicamera(), ModePicamera(OV5647_MODES)
        cameras = iter((imx219, ov5647))
        backend = Picamera2CameraBackend(lambda: next(cameras))
        backend.start()
        backend.stop()
        backend.start()
        self.assertEqual(imx219.effective["sensor"]["output_size"], (1640, 1232))
        self.assertEqual(ov5647.effective["sensor"]["output_size"], (640, 480))
        backend.capture_frame()
        backend.stop()

    def test_effective_output_size_change_fails_and_closes_camera(self):
        class AdjustedCamera(ModePicamera):
            def camera_configuration(self):
                configuration = super().camera_configuration()
                configuration["main"] = {"size": (320, 240)}
                return configuration

        camera = AdjustedCamera()
        with self.assertRaisesRegex(Picamera2DeviceUnavailableError, "output size"):
            Picamera2CameraBackend(lambda: camera).start()
        self.assertEqual(camera.operations[-1], ("close",))

    def test_selected_configuration_failure_does_not_trigger_automatic_retry(self):
        class RejectingCamera(ModePicamera):
            def configure(self, configuration):
                if "sensor" in configuration:
                    self.configure_error = RuntimeError("sensor configure failed")
                super().configure(configuration)

        camera = RejectingCamera()
        with self.assertRaisesRegex(Picamera2DeviceUnavailableError, "sensor configure failed"):
            Picamera2CameraBackend(lambda: camera).start()
        self.assertEqual(sum(op[0] == "configure" for op in camera.operations), 2)
        self.assertEqual(camera.operations[-1], ("close",))

    def test_stop_failure_still_closes_camera(self):
        camera = ModePicamera()
        backend = Picamera2CameraBackend(lambda: camera)
        backend.start()
        with patch.object(camera, "stop", side_effect=RuntimeError("stop failed")):
            with self.assertRaisesRegex(RuntimeError, "stop failed"):
                backend.stop()
        self.assertEqual(camera.operations[-1], ("close",))
        self.assertFalse(backend.is_running)

    def test_driver_ignoring_requested_mode_restores_automatic_and_logs_fallback(self):
        camera = ModePicamera()
        camera.ignore_sensor = True
        with self.assertLogs(CAMERA_LOGGER, level="INFO") as logs:
            Picamera2CameraBackend(lambda: camera).start()
        self.assertEqual(camera.effective["sensor"]["output_size"], (640, 480))
        self.assertEqual(sum(op[0] == "configure" for op in camera.operations), 3)
        self.assertIn("effective_mode_unverified", logs.output[0])
        self.assertIn("automatic_fallback=True", logs.output[0])

    def test_effective_crop_can_disprove_requested_coverage(self):
        camera = ModePicamera()
        camera.crop_override = (1000, 752, 1280, 960)
        with self.assertLogs(CAMERA_LOGGER, level="INFO") as logs:
            Picamera2CameraBackend(lambda: camera).start()
        self.assertIn("effective_mode_unverified", logs.output[0])

    def test_first_capture_reports_actual_crop_without_extra_capture(self):
        camera = ModePicamera()
        backend = Picamera2CameraBackend(lambda: camera)
        backend.start()
        camera.capture_crop = (0, 2, 3280, 2460)  # Normal 4:3 aspect crop.
        with self.assertLogs(CAMERA_LOGGER, level="INFO") as logs:
            backend.capture_frame()
            backend.capture_frame()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("coverage=ok", logs.output[0])
        self.assertEqual(sum(op[0] == "capture" for op in camera.operations), 2)

    def test_first_capture_warns_if_actual_crop_reduces_coverage(self):
        camera = ModePicamera()
        backend = Picamera2CameraBackend(lambda: camera)
        backend.start()
        camera.capture_crop = (1000, 752, 1280, 960)
        with self.assertLogs(CAMERA_LOGGER, level="WARNING") as logs:
            backend.capture_frame()
        self.assertIn("coverage=reduced", logs.output[0])

    def test_configuration_failure_is_not_retried_as_compatibility_fallback(self):
        camera = ModePicamera()
        camera.configure_error = RuntimeError("allocation failed")
        backend = Picamera2CameraBackend(lambda: camera)
        with self.assertRaisesRegex(Picamera2DeviceUnavailableError, "allocation failed"):
            backend.start()
        self.assertEqual(sum(op[0] == "configure" for op in camera.operations), 1)
        self.assertEqual(camera.operations[-1], ("close",))
        self.assertFalse(backend.is_running)

    def test_capture_failure_propagates_and_stop_still_cleans_up(self):
        camera = ModePicamera()
        backend = Picamera2CameraBackend(lambda: camera)
        backend.start()
        camera.capture_error = RuntimeError("capture failed")
        with self.assertRaisesRegex(RuntimeError, "capture failed"):
            backend.capture_frame()
        backend.stop()
        self.assertEqual(camera.operations[-2:], [("stop",), ("close",)])


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


class RecordingArbiter(ResourceArbiter):
    def __init__(self):
        super().__init__()
        self.acquisitions = []

    def acquire(self, resource, owner):
        self.acquisitions.append((resource, owner))
        return super().acquire(resource, owner)


class ApplicationCameraTests(unittest.IsolatedAsyncioTestCase):
    def application(self, camera=None, events=None, body_backend=None, **kwargs):
        return RobotApplication(
            RobotProfile("test", "Test"),
            VirtualHardwareBackend(),
            events=events,
            platform_provider=PlatformProvider(),
            body_backend=body_backend,
            camera_backend=camera,
            **kwargs,
        )

    async def test_capture_uses_direct_owner_and_releases_camera(self):
        camera = FakeCamera()
        resources = RecordingArbiter()
        app = self.application(camera, resource_arbiter=resources)
        await app.start()

        frame = app.capture_camera_frame()

        self.assertIs(frame, camera.frame)
        self.assertEqual(app.observability.snapshot()["metrics"]["camera_captures"], 1)
        self.assertEqual(resources.acquisitions, [
            (CAMERA_RESOURCE, CAMERA_CAPTURE_OWNER)
        ])
        self.assertIsNone(resources.lease_for(CAMERA_RESOURCE))
        await app.stop()

    async def test_busy_camera_blocks_backend_and_preserves_foreign_lease(self):
        camera = FakeCamera()
        app = self.application(camera)
        await app.start()
        foreign = app.resources.acquire(
            CAMERA_RESOURCE, ResourceOwner("runtime", "other_camera_user")
        )

        with self.assertRaises(ResourceBusyError):
            app.capture_camera_frame()
        self.assertEqual(camera.captures, 0)
        self.assertIs(app.resources.lease_for(CAMERA_RESOURCE), foreign)

        app.resources.release(foreign)
        self.assertIs(app.capture_camera_frame(), camera.frame)
        self.assertEqual(camera.captures, 1)
        await app.stop()

    async def test_capture_failure_releases_camera(self):
        camera = FakeCamera()
        app = self.application(camera)
        await app.start()
        with patch.object(camera, "capture_frame", side_effect=RuntimeError("broken")):
            with self.assertRaisesRegex(RuntimeError, "broken"):
                app.capture_camera_frame()
        self.assertIsNone(app.resources.lease_for(CAMERA_RESOURCE))
        await app.stop()

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
    def setUp(self) -> None:
        self.history_patch = patch(
            "embodied_runtime.cli.start_run", side_effect=OSError("unavailable")
        )
        self.history_patch.start()

    def tearDown(self) -> None:
        self.history_patch.stop()

    def test_default_is_none_and_physical_selection_is_explicit(self):
        args = build_parser().parse_args([])
        self.assertEqual(args.camera, "none")
        self.assertIsNone(args.camera_test)
        selected = build_parser().parse_args(["--camera", "picamera2"])
        backend = build_camera_backend(selected)
        self.assertIsInstance(backend, Picamera2CameraBackend)
        self.assertFalse(backend.is_running)

    def test_auto_camera_keeps_usable_picamera_open_for_application_start(self):
        camera = FakeCamera()
        args = build_parser().parse_args(["--camera", "auto"])
        with patch(
            "embodied_runtime.cli.Picamera2CameraBackend", return_value=camera,
        ):
            self.assertIs(build_camera_backend(args), camera)
        self.assertEqual(camera.starts, 1)
        self.assertTrue(camera.is_running)

    def test_auto_camera_resolves_unavailable_picamera_to_none(self):
        camera = FakeCamera(fail_start=True)
        args = build_parser().parse_args(["--camera", "auto"])
        with patch(
            "embodied_runtime.cli.Picamera2CameraBackend", return_value=camera,
        ), patch.object(
            camera, "start", side_effect=Picamera2DeviceUnavailableError("missing"),
        ):
            self.assertIsNone(build_camera_backend(args))

    def test_none_camera_remains_absent(self):
        args = build_parser().parse_args(["--camera", "none"])
        with patch("embodied_runtime.cli.Picamera2CameraBackend") as constructor:
            self.assertIsNone(build_camera_backend(args))
        constructor.assert_not_called()

    def test_camera_test_validation(self):
        for argv in (["--camera", "picamera2", "--camera-test", "out.jpg"],
                     ["--diagnostics", "--camera-test", "out.jpg"]):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit):
                main(argv)

    def test_diagnostic_writes_exactly_one_returned_frame(self):
        camera = FakeCamera()
        application_captures = []
        capture = RobotApplication.capture_camera_frame

        def recording_capture(application):
            application_captures.append(application)
            return capture(application)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "frame.jpg"
            with (
                patch("embodied_runtime.cli.build_camera_backend", return_value=camera),
                patch.object(
                    RobotApplication, "capture_camera_frame", recording_capture
                ),
            ):
                with patch("builtins.print") as printer:
                    result = main([
                        "--camera", "picamera2", "--diagnostics",
                        "--camera-test", str(output),
                    ])
            self.assertEqual(result, 0)
            self.assertEqual(output.read_bytes(), JPEG)
            self.assertEqual(camera.captures, 1)
            self.assertEqual(len(application_captures), 1)
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
