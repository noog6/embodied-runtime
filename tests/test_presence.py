import asyncio
from dataclasses import FrozenInstanceError
import unittest

from embodied_runtime.app import RobotApplication
from embodied_runtime.events import PresenceChanged
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile
from embodied_runtime.state import PresenceState
from tests.test_platform import snapshot


class PlatformProvider:
    def snapshot(self):
        return snapshot()


class PresenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = RobotApplication(
            RobotProfile("test", "Test"),
            VirtualHardwareBackend(),
            platform_provider=PlatformProvider(),
        )

    async def asyncTearDown(self):
        await self.app.stop()

    async def test_presence_state_is_immutable_and_initially_unknown(self):
        self.assertIsNone(self.app.runtime_state.presence)
        state = PresenceState(True, "test")
        with self.assertRaises(FrozenInstanceError):
            state.present = False  # type: ignore[misc]

    async def test_observation_requires_running_and_nonempty_source(self):
        with self.assertRaisesRegex(RuntimeError, "running application"):
            await self.app.observe_presence(present=True, source="test")
        await self.app.start()
        with self.assertRaisesRegex(ValueError, "non-empty"):
            await self.app.observe_presence(present=True, source=" ")

    async def test_observation_rejects_non_boolean_without_state_or_event(self):
        delivered = asyncio.Event()

        async def handler(_event: PresenceChanged) -> None:
            delivered.set()

        self.app.events.subscribe(PresenceChanged, handler)
        await self.app.start()
        for value in ("true", "false", 1, 0, None, [], object()):
            with self.subTest(value=value), self.assertRaisesRegex(TypeError, "bool"):
                await self.app.observe_presence(present=value, source="test")  # type: ignore[arg-type]
            self.assertIsNone(self.app.runtime_state.presence)
        await asyncio.sleep(0)
        self.assertFalse(delivered.is_set())

    async def test_state_precedes_event_and_equal_values_are_deduplicated(self):
        events = []
        states = []
        delivered = asyncio.Event()

        async def handler(event: PresenceChanged) -> None:
            events.append(event)
            states.append(self.app.runtime_state.presence)
            delivered.set()

        self.app.events.subscribe(PresenceChanged, handler)
        await self.app.start()

        await self.app.observe_presence(present=True, source="virtual_scenario")
        await delivered.wait()
        self.assertEqual(states, [PresenceState(True, "virtual_scenario")])
        self.assertEqual(events[0].source, "virtual_scenario")
        self.assertIsNone(events[0].previous_present)
        self.assertTrue(events[0].present)

        delivered.clear()
        await self.app.observe_presence(present=True, source="updated_source")
        await asyncio.sleep(0)
        self.assertFalse(delivered.is_set())
        self.assertEqual(len(events), 1)
        self.assertEqual(self.app.runtime_state.presence, PresenceState(True, "updated_source"))

        await self.app.observe_presence(present=False, source="virtual_scenario")
        await delivered.wait()
        self.assertEqual(len(events), 2)
        self.assertTrue(events[1].previous_present)
        self.assertFalse(events[1].present)
        self.assertEqual(states[1], PresenceState(False, "virtual_scenario"))
