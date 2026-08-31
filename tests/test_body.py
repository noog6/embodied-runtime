import asyncio
from dataclasses import FrozenInstanceError
import math
import unittest

from embodied_runtime.app import RobotApplication
from embodied_runtime.body.base import BodyBackend
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.events import ApplicationStarted, EventBus
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile
from embodied_runtime.state import BodyState, LifecycleState
from tests.test_platform import snapshot


class PlatformProvider:
    def snapshot(self):
        return snapshot()


class RecordingHardware(VirtualHardwareBackend):
    def __init__(self, calls: list[str]) -> None:
        super().__init__()
        self.calls = calls

    def start(self) -> None:
        self.calls.append("hardware.start")
        super().start()

    def stop(self) -> None:
        self.calls.append("hardware.stop")
        super().stop()


class RecordingBody(BodyBackend):
    identifier = "recording"
    is_physical = False
    capabilities = ("orientation",)

    def __init__(
        self,
        calls: list[str],
        *,
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
    ) -> None:
        self.calls = calls
        self.start_error = start_error
        self.stop_error = stop_error

    async def start(self) -> BodyState:
        self.calls.append("body.start")
        if self.start_error is not None:
            raise self.start_error
        return BodyState(0.0, 0.0)

    async def stop(self) -> None:
        self.calls.append("body.stop")
        if self.stop_error is not None:
            raise self.stop_error

    async def set_orientation(
        self, yaw_degrees: float, pitch_degrees: float
    ) -> BodyState:
        return BodyState(yaw_degrees, pitch_degrees)


class RecordingReflex:
    identifier = "recording"

    def __init__(self, calls, *, start_error=None, stop_error=None):
        self.calls = calls
        self.start_error = start_error
        self.stop_error = stop_error
        self.active = False

    async def start(self, events, capabilities):
        self.calls.append("reflex.start")
        self.active = True
        if self.start_error is not None:
            raise self.start_error

    async def stop(self):
        self.calls.append("reflex.stop")
        self.active = False
        if self.stop_error is not None:
            raise self.stop_error


class FailingStopEventBus(EventBus):
    def __init__(self, calls):
        super().__init__()
        self.calls = calls

    async def stop(self):
        self.calls.append("events.stop")
        await super().stop()
        raise RuntimeError("event cleanup failed")


def application(hardware, body, *, events=None, reflexes=()):
    return RobotApplication(
        RobotProfile("test", "Test"),
        hardware,
        events=events,
        platform_provider=PlatformProvider(),
        body_backend=body,
        reflexes=reflexes,
    )


class VirtualBodyBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_neutral_start_and_idempotent_stop(self):
        body = VirtualBodyBackend()
        self.assertEqual(body.identifier, "virtual")
        self.assertFalse(body.is_physical)
        self.assertEqual(body.capabilities, ("orientation",))
        self.assertEqual(await body.start(), BodyState(0.0, 0.0))
        await body.stop()
        await body.stop()

    async def test_body_state_is_immutable(self):
        state = BodyState(0.0, 0.0)
        with self.assertRaises(FrozenInstanceError):
            state.yaw_degrees = 1.0  # type: ignore[misc]

    async def test_valid_orientation_and_boundaries(self):
        body = VirtualBodyBackend()
        await body.start()
        for yaw, pitch in [(-180, -90), (180, 90), (30, -10)]:
            with self.subTest(yaw=yaw, pitch=pitch):
                self.assertEqual(
                    await body.set_orientation(yaw, pitch),
                    BodyState(float(yaw), float(pitch)),
                )

    async def test_invalid_orientation_is_rejected(self):
        body = VirtualBodyBackend()
        await body.start()
        values = [
            (181, 0), (-181, 0), (0, 91), (0, -91),
            (math.nan, 0), (math.inf, 0), (0, -math.inf),
        ]
        for yaw, pitch in values:
            with self.subTest(yaw=yaw, pitch=pitch), self.assertRaises(ValueError):
                await body.set_orientation(yaw, pitch)


class ApplicationBodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_started_handler_sees_running_neutral_body(self):
        app = application(VirtualHardwareBackend(), VirtualBodyBackend())
        observed = []
        delivered = asyncio.Event()

        async def handler(_event: ApplicationStarted) -> None:
            observed.append((app.state, app.runtime_state.body))
            delivered.set()

        app.events.subscribe(ApplicationStarted, handler)
        await app.start()
        await delivered.wait()
        self.assertEqual(observed, [(LifecycleState.RUNNING, BodyState(0.0, 0.0))])
        await app.stop()

    async def test_started_handler_sees_reflex_already_active(self):
        calls = []
        reflex = RecordingReflex(calls)
        app = application(
            VirtualHardwareBackend(), VirtualBodyBackend(), reflexes=(reflex,)
        )
        observed = []
        delivered = asyncio.Event()

        async def handler(_event: ApplicationStarted) -> None:
            observed.append((app.state, app.runtime_state.body, reflex.active))
            delivered.set()

        app.events.subscribe(ApplicationStarted, handler)
        await app.start()
        await delivered.wait()
        self.assertEqual(
            observed, [(LifecycleState.RUNNING, BodyState(0.0, 0.0), True)]
        )
        await app.stop()

    async def test_orientation_requires_running_and_body_backend(self):
        app = application(VirtualHardwareBackend(), VirtualBodyBackend())
        with self.assertRaisesRegex(RuntimeError, "running application"):
            await app.set_body_orientation(yaw_degrees=0, pitch_degrees=0)
        no_body = RobotApplication(
            RobotProfile("test", "Test"),
            VirtualHardwareBackend(),
            platform_provider=PlatformProvider(),
        )
        await no_body.start()
        with self.assertRaisesRegex(RuntimeError, "No body backend"):
            await no_body.set_body_orientation(yaw_degrees=0, pitch_degrees=0)
        await no_body.stop()

    async def test_success_replaces_state_and_failure_preserves_it(self):
        app = application(VirtualHardwareBackend(), VirtualBodyBackend())
        await app.start()
        previous_runtime = app.runtime_state
        await app.set_body_orientation(yaw_degrees=30, pitch_degrees=-10)
        self.assertEqual(app.runtime_state.body, BodyState(30.0, -10.0))
        self.assertEqual(previous_runtime.body, BodyState(0.0, 0.0))
        previous_body = app.runtime_state.body
        with self.assertRaises(ValueError):
            await app.set_body_orientation(yaw_degrees=181, pitch_degrees=0)
        self.assertIs(app.runtime_state.body, previous_body)
        await app.stop()

    async def test_body_start_failure_cleans_up_without_running(self):
        calls: list[str] = []
        hardware = RecordingHardware(calls)
        body = RecordingBody(calls, start_error=RuntimeError("body start failed"))
        events = EventBus()
        app = application(hardware, body, events=events)

        with self.assertRaisesRegex(RuntimeError, "body start failed"):
            await app.start()

        self.assertEqual(app.state, LifecycleState.STOPPED)
        self.assertEqual(calls, ["hardware.start", "body.start", "body.stop", "hardware.stop"])
        self.assertFalse(hardware.is_running)
        self.assertFalse(events.is_running)
        self.assertFalse(app._platform_monitor.is_running)

    async def test_cleanup_failure_preserves_original_start_failure(self):
        calls: list[str] = []
        hardware = RecordingHardware(calls)
        original = RuntimeError("original body start failure")
        body = RecordingBody(
            calls,
            start_error=original,
            stop_error=RuntimeError("body cleanup failure"),
        )
        app = application(hardware, body)

        with self.assertLogs("embodied_runtime.app", level="ERROR"):
            with self.assertRaises(RuntimeError) as raised:
                await app.start()

        self.assertIs(raised.exception, original)
        self.assertIn("hardware.stop", calls)
        self.assertFalse(hardware.is_running)
        self.assertFalse(app.events.is_running)
        self.assertEqual(app.state, LifecycleState.STOPPED)

    async def test_normal_shutdown_stops_body_before_hardware(self):
        calls: list[str] = []
        app = application(RecordingHardware(calls), RecordingBody(calls))
        await app.start()
        calls.clear()
        await app.stop()
        self.assertEqual(calls, ["body.stop", "hardware.stop"])

    async def test_shutdown_stops_reflex_before_body_and_hardware(self):
        calls: list[str] = []
        reflex = RecordingReflex(calls)
        app = application(
            RecordingHardware(calls), RecordingBody(calls), reflexes=(reflex,)
        )
        await app.start()
        calls.clear()
        await app.stop()
        self.assertEqual(calls, ["reflex.stop", "body.stop", "hardware.stop"])

    async def test_reflex_start_failure_cleans_all_dependencies(self):
        calls: list[str] = []
        hardware = RecordingHardware(calls)
        body = RecordingBody(calls)
        original = RuntimeError("reflex start failed")
        reflex = RecordingReflex(
            calls, start_error=original, stop_error=RuntimeError("cleanup failed")
        )
        app = application(hardware, body, reflexes=(reflex,))
        with self.assertLogs("embodied_runtime.app", level="ERROR"):
            with self.assertRaises(RuntimeError) as raised:
                await app.start()
        self.assertIs(raised.exception, original)
        self.assertEqual(
            calls,
            ["hardware.start", "body.start", "reflex.start", "reflex.stop",
             "body.stop", "hardware.stop"],
        )
        self.assertEqual(app.state, LifecycleState.STOPPED)
        self.assertFalse(app.events.is_running)
        self.assertFalse(app._platform_monitor.is_running)

    async def test_event_cleanup_failure_preserves_reflex_start_failure(self):
        calls: list[str] = []
        hardware = RecordingHardware(calls)
        body = RecordingBody(calls)
        events = FailingStopEventBus(calls)
        original = RuntimeError("reflex start failed")
        reflex = RecordingReflex(calls, start_error=original)
        app = application(
            hardware, body, events=events, reflexes=(reflex,)
        )

        with self.assertLogs("embodied_runtime.app", level="ERROR") as logs:
            with self.assertRaises(RuntimeError) as raised:
                await app.start()

        self.assertIs(raised.exception, original)
        self.assertEqual(
            calls,
            ["hardware.start", "body.start", "reflex.start", "reflex.stop",
             "body.stop", "hardware.stop", "events.stop"],
        )
        self.assertTrue(any("[EVENT] cleanup_failed" in line for line in logs.output))
        self.assertEqual(app.state, LifecycleState.STOPPED)
        self.assertFalse(events.is_running)
        self.assertFalse(app._platform_monitor.is_running)

    async def test_reflex_stop_failure_does_not_prevent_shutdown(self):
        calls: list[str] = []
        hardware = RecordingHardware(calls)
        reflex = RecordingReflex(calls, stop_error=RuntimeError("stop failed"))
        app = application(
            hardware, RecordingBody(calls), reflexes=(reflex,)
        )
        await app.start()
        calls.clear()
        with self.assertLogs("embodied_runtime.app", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "stop failed"):
                await app.stop()
        self.assertEqual(calls, ["reflex.stop", "body.stop", "hardware.stop"])
        self.assertEqual(app.state, LifecycleState.STOPPED)
        self.assertFalse(hardware.is_running)
        self.assertFalse(app.events.is_running)

    async def test_body_stop_failure_still_completes_remaining_cleanup(self):
        calls: list[str] = []
        hardware = RecordingHardware(calls)
        body = RecordingBody(calls, stop_error=RuntimeError("body stop failed"))
        events = EventBus()
        app = application(hardware, body, events=events)
        await app.start()
        calls.clear()

        with self.assertLogs("embodied_runtime.app", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "body stop failed"):
                await app.stop()

        self.assertEqual(calls, ["body.stop", "hardware.stop"])
        self.assertFalse(hardware.is_running)
        self.assertFalse(events.is_running)
        self.assertEqual(app.state, LifecycleState.STOPPED)
