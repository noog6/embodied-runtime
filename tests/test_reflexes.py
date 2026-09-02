import asyncio
import unittest

from embodied_runtime.app import RobotApplication
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cognition import CognitionToolCall
from embodied_runtime.events import EventBus, PresenceChanged
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile
from embodied_runtime.reflexes import PresenceCenteringReflex
from embodied_runtime.state import BodyState, LifecycleState
from tests.test_platform import snapshot


class PlatformProvider:
    def snapshot(self):
        return snapshot()


class RecordingCapabilities:
    def __init__(self, error=None):
        self.calls = []
        self.called = asyncio.Event()
        self.error = error

    async def set_body_orientation(self, **arguments):
        self.calls.append(arguments)
        self.called.set()
        if self.error is not None:
            raise self.error


class PresenceCenteringReflexTests(unittest.IsolatedAsyncioTestCase):
    async def test_identifier_start_subscription_and_idempotent_stop(self):
        bus = EventBus()
        capabilities = RecordingCapabilities()
        reflex = PresenceCenteringReflex()
        self.assertEqual(reflex.identifier, "presence_centering")
        await bus.start()
        await reflex.start(bus, capabilities)
        self.assertEqual(len(bus._subscriptions), 1)
        self.assertIs(bus._subscriptions[0].event_type, PresenceChanged)
        await reflex.stop()
        await reflex.stop()
        self.assertEqual(bus._subscriptions, [])
        await bus.stop()

    async def test_true_from_any_source_uses_semantic_capability(self):
        for source in ("virtual_scenario", "radar"):
            with self.subTest(source=source):
                bus = EventBus()
                capabilities = RecordingCapabilities()
                reflex = PresenceCenteringReflex()
                await bus.start()
                await reflex.start(bus, capabilities)
                await bus.publish(PresenceChanged(
                    source=source, previous_present=False, present=True
                ))
                await capabilities.called.wait()
                self.assertEqual(capabilities.calls, [{
                    "yaw_degrees": 0.0, "pitch_degrees": 0.0,
                    "source": "reflex:presence_centering",
                }])
                await reflex.stop()
                await bus.stop()

    async def test_false_does_nothing(self):
        bus = EventBus()
        capabilities = RecordingCapabilities()
        reflex = PresenceCenteringReflex()
        await bus.start()
        await reflex.start(bus, capabilities)
        await bus.publish(PresenceChanged(
            source="test", previous_present=True, present=False
        ))
        await asyncio.sleep(0)
        self.assertEqual(capabilities.calls, [])
        await reflex.stop()
        await bus.stop()

    async def test_capability_failure_is_isolated(self):
        bus = EventBus()
        capabilities = RecordingCapabilities(RuntimeError("rejected"))
        reflex = PresenceCenteringReflex()
        await bus.start()
        await reflex.start(bus, capabilities)
        with self.assertLogs("embodied_runtime.reflexes.presence", level="ERROR"):
            await bus.publish(PresenceChanged(
                source="test", previous_present=False, present=True
            ))
            await capabilities.called.wait()
        self.assertTrue(bus.is_running)
        await reflex.stop()
        await bus.stop()


class CountingBody(VirtualBodyBackend):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.changed = asyncio.Event()

    async def set_orientation(self, yaw_degrees, pitch_degrees):
        self.calls.append((yaw_degrees, pitch_degrees))
        result = await super().set_orientation(yaw_degrees, pitch_degrees)
        self.changed.set()
        return result


class ReflexApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.body = CountingBody()
        self.app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=PlatformProvider(), body_backend=self.body,
            reflexes=(PresenceCenteringReflex(),),
        )
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.stop()

    async def wait_for_body_calls(self, count):
        while len(self.body.calls) < count:
            self.body.changed.clear()
            if len(self.body.calls) < count:
                await self.body.changed.wait()

    async def test_transitions_center_deduplicate_and_retrigger(self):
        await self.app.set_body_orientation(yaw_degrees=30, pitch_degrees=-10)
        self.body.calls.clear()
        await self.app.observe_presence(present=True, source="radar")
        await self.wait_for_body_calls(1)
        self.assertEqual(self.body.calls, [(0.0, 0.0)])
        self.assertEqual(self.app.runtime_state.body, BodyState(0.0, 0.0))

        await self.app.observe_presence(present=True, source="vision")
        await asyncio.sleep(0)
        self.assertEqual(len(self.body.calls), 1)
        await self.app.observe_presence(present=False, source="vision")
        await asyncio.sleep(0)
        self.assertEqual(len(self.body.calls), 1)

        await self.app.set_body_orientation(yaw_degrees=-45, pitch_degrees=15)
        await self.app.observe_presence(present=True, source="tof")
        await self.wait_for_body_calls(3)
        self.assertEqual(self.body.calls[-1], (0.0, 0.0))
        self.assertEqual(self.app.runtime_state.body, BodyState(0.0, 0.0))

    async def test_failed_request_preserves_authoritative_body_and_runtime(self):
        await self.app.set_body_orientation(yaw_degrees=30, pitch_degrees=-10)
        previous = self.app.runtime_state.body
        original = self.app.set_body_orientation
        called = asyncio.Event()

        async def fail(**_arguments):
            called.set()
            raise RuntimeError("unavailable")

        self.app.set_body_orientation = fail
        with self.assertLogs("embodied_runtime.reflexes.presence", level="ERROR"):
            await self.app.observe_presence(present=True, source="test")
            await called.wait()
        self.assertIs(self.app.runtime_state.body, previous)
        self.assertEqual(self.app.state, LifecycleState.RUNNING)
        self.assertTrue(self.app.events.is_running)
        self.app.set_body_orientation = original

    async def test_local_reflex_overrides_cognition_requested_pose(self):
        result = await self.app._execute_cognition_tool(CognitionToolCall(
            "orient_body", '{"yaw_degrees":35,"pitch_degrees":-10}'
        ))
        self.assertIn('"status": "applied"', result.output)
        self.assertEqual(self.app.runtime_state.body, BodyState(35.0, -10.0))
        await self.app.observe_presence(present=True, source="test")
        await self.wait_for_body_calls(2)
        self.assertEqual(self.app.runtime_state.body, BodyState(0.0, 0.0))
