import asyncio
import unittest

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.attention import INITIATIVE_REQUEST
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cognition import CognitionError, TextCognitionBackend
from embodied_runtime.events import BodyOrientationChanged
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile
from embodied_runtime.reflexes import PresenceCenteringReflex
from embodied_runtime.state import BodyState, LifecycleState
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


class FakeCognition(TextCognitionBackend):
    identifier = "fake"

    def __init__(self, *, blocked=False, error=None):
        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.blocked = blocked
        self.error = error

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tools, tool_executor,
                              refreshed_instructions))
        self.started.set()
        if self.blocked:
            await self.release.wait()
        if self.error:
            raise self.error
        return "The current body differs; no action was taken."


class AttentionTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, backend=None, *, enabled=True, reflexes=()):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=enabled),
            platform_provider=Platform(), body_backend=VirtualBodyBackend(),
            cognition_backend=backend, reflexes=reflexes,
        )

    async def test_body_event_follows_state_and_omits_noop_and_failure(self):
        app = self.make_app()
        received = []
        delivered = asyncio.Event()

        async def handler(event):
            received.append((event, app.runtime_state.body))
            delivered.set()

        app.events.subscribe(BodyOrientationChanged, handler)
        await app.start()
        await app.set_body_orientation(yaw_degrees=10, pitch_degrees=-5,
                                       source="console")
        await delivered.wait()
        event, state_at_delivery = received[0]
        self.assertEqual((event.previous_yaw_degrees, event.previous_pitch_degrees,
                          event.yaw_degrees, event.pitch_degrees, event.source),
                         (0.0, 0.0, 10.0, -5.0, "console"))
        self.assertEqual(state_at_delivery, BodyState(10.0, -5.0))
        delivered.clear()
        await app.set_body_orientation(yaw_degrees=10, pitch_degrees=-5)
        await asyncio.sleep(0)
        self.assertEqual(len(received), 1)
        previous = app.runtime_state.body
        original = app.body_backend.set_orientation
        async def fail(*args):
            raise RuntimeError("failed")
        app.body_backend.set_orientation = fail
        with self.assertRaises(RuntimeError):
            await app.set_body_orientation(yaw_degrees=20, pitch_degrees=0)
        self.assertIs(app.runtime_state.body, previous)
        self.assertEqual(len(received), 1)
        app.body_backend.set_orientation = original
        await app.stop()

    async def test_reflex_wakes_fresh_read_only_request_without_memory_mutation(self):
        backend = FakeCognition()
        app = self.make_app(backend, reflexes=(PresenceCenteringReflex(),))
        await app.start()
        await app.set_body_orientation(yaw_degrees=35, pitch_degrees=-10)
        app.set_goal("Keep body at 35/-10")
        app.working_memory.append("prior operator", "prior response")
        memory = app.working_memory.snapshot()
        await app.observe_presence(present=True, source="test")
        await asyncio.wait_for(backend.started.wait(), 1)
        while app.attention.status().state == "in_flight":
            await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 1)
        message, instructions, tools, executor, refresh = backend.requests[0]
        self.assertEqual(message, INITIATIVE_REQUEST)
        self.assertIn("yaw_deg: 0.0", instructions)
        self.assertIn("description: \"Keep body at 35/-10\"", instructions)
        self.assertIn("prior operator", instructions)
        self.assertIn("Attention stimulus", instructions)
        self.assertIn("previous_yaw_deg: 35.0", instructions)
        self.assertIn("previous_pitch_deg: -10.0", instructions)
        self.assertIn("source: reflex:presence_centering", instructions)
        self.assertEqual(tools, ())
        self.assertIsNone(executor)
        self.assertIsNone(refresh)
        self.assertEqual(app.runtime_state.body, BodyState(0.0, 0.0))
        self.assertEqual(app.active_goal.description, "Keep body at 35/-10")
        self.assertEqual(app.working_memory.snapshot(), memory)
        self.assertEqual(app.attention.status().state, "completed")
        await app.stop()

    async def test_disabled_nonreflex_and_no_goal_do_not_wake(self):
        for enabled, goal, source in (
            (False, True, "reflex:test"),
            (True, False, "reflex:test"),
            (True, True, "cognition"),
            (True, True, "console"),
            (True, True, "application"),
        ):
            with self.subTest(enabled=enabled, goal=goal, source=source):
                backend = FakeCognition()
                app = self.make_app(backend, enabled=enabled)
                await app.start()
                if goal:
                    app.set_goal("opaque")
                await app.set_body_orientation(yaw_degrees=1, pitch_degrees=2,
                                               source=source)
                await asyncio.sleep(0.01)
                self.assertEqual(backend.requests, [])
                await app.stop()

    async def test_second_event_is_suppressed_not_queued(self):
        backend = FakeCognition(blocked=True)
        app = self.make_app(backend)
        await app.start()
        app.set_goal("opaque")
        await app.set_body_orientation(yaw_degrees=1, pitch_degrees=0,
                                       source="reflex:a")
        await backend.started.wait()
        await app.set_body_orientation(yaw_degrees=2, pitch_degrees=0,
                                       source="reflex:b")
        await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 1)
        backend.release.set()
        while app.attention.status().state == "in_flight":
            await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 1)
        await app.stop()

    async def test_failure_is_nonfatal_no_retry_and_later_operator_works(self):
        backend = FakeCognition(error=CognitionError("bad"))
        app = self.make_app(backend)
        await app.start()
        app.set_goal("opaque")
        memory = app.working_memory.snapshot()
        await app.set_body_orientation(yaw_degrees=1, pitch_degrees=0,
                                       source="reflex:test")
        await backend.started.wait()
        while app.attention.status().state == "in_flight":
            await asyncio.sleep(0)
        self.assertEqual(app.state, LifecycleState.RUNNING)
        self.assertEqual(app.attention.status().state, "failed")
        self.assertEqual(app.working_memory.snapshot(), memory)
        backend.error = None
        self.assertTrue(await app.request_cognition("operator"))
        self.assertEqual(len(app.working_memory), 1)
        await app.stop()

    async def test_stop_cancels_blocking_initiative_and_closes_subscription(self):
        backend = FakeCognition(blocked=True)
        app = self.make_app(backend)
        await app.start()
        app.set_goal("opaque")
        await app.set_body_orientation(yaw_degrees=1, pitch_degrees=0,
                                       source="reflex:test")
        await backend.started.wait()
        task = app.attention._task
        await asyncio.wait_for(app.stop(), 1)
        self.assertTrue(task.cancelled())
        self.assertEqual(app.events._subscriptions, [])
