import asyncio
from dataclasses import FrozenInstanceError
import unittest

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.attention import AttentionStimulus
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cognition import TextCognitionBackend
from embodied_runtime.events import (
    BodyOrientationChanged, MemoryPressureCleared, MemoryPressureRaised, PresenceChanged,
    ThermalWarningCleared, ThermalWarningRaised,
)
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.observations import (
    SemanticObservation, SemanticObservationFact,
    observation_from_body_orientation,
)
from embodied_runtime.profile import RobotProfile
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot(cpu_temperature_celsius=30.0)


class RecordingCognition(TextCognitionBackend):
    identifier = "recording"

    def __init__(self, *, blocked=False):
        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.blocked = blocked

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tools))
        self.started.set()
        if self.blocked:
            await self.release.wait()
        return "observed"


def thermal(raised=True):
    cls = ThermalWarningRaised if raised else ThermalWarningCleared
    threshold = ({"warning_threshold_celsius": 80.0} if raised else
                 {"clear_threshold_celsius": 75.0})
    return cls(source="platform_monitor", cpu_temperature_celsius=81.0, **threshold)


def memory(raised=True):
    cls = MemoryPressureRaised if raised else MemoryPressureCleared
    threshold = ({"pressure_threshold_ratio": .1} if raised else
                 {"clear_threshold_ratio": .15})
    return cls(source="platform_monitor", memory_available_bytes=10,
               memory_total_bytes=100, available_ratio=.1, **threshold)


class SemanticObservationTests(unittest.TestCase):
    def test_immutable_ordered_rendering_and_no_event_storage(self):
        facts = (SemanticObservationFact("first", "1"),
                 SemanticObservationFact("second", "2"))
        observation = SemanticObservation("example", "test", facts)
        with self.assertRaises(FrozenInstanceError):
            observation.kind = "changed"
        with self.assertRaises(FrozenInstanceError):
            facts[0].value = "changed"
        rendered = AttentionStimulus(observation).render()
        self.assertLess(rendered.index("first: 1"), rendered.index("second: 2"))
        self.assertEqual(observation.__slots__, ("kind", "source", "facts"))

    def test_body_event_maps_values_without_retaining_event(self):
        event = BodyOrientationChanged(
            source="reflex:test", previous_yaw_degrees=12.0,
            previous_pitch_degrees=-3.0, yaw_degrees=0.0, pitch_degrees=1.0,
        )
        observation = observation_from_body_orientation(event)
        self.assertEqual((observation.kind, observation.source),
                         ("body_orientation_changed", "reflex:test"))
        self.assertEqual(
            tuple((fact.name, fact.value) for fact in observation.facts),
            (("previous_yaw_deg", "12.0"), ("previous_pitch_deg", "-3.0"),
             ("yaw_deg", "0.0"), ("pitch_deg", "1.0")),
        )
        self.assertFalse(any(value is event for value in (
            observation.kind, observation.source, observation.facts
        )))


class PlatformAttentionTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, backend, *, platform_attention=True):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(
                initiative_enabled=True,
                initiative_platform_attention_enabled=platform_attention,
            ),
            platform_provider=Platform(), body_backend=VirtualBodyBackend(),
            cognition_backend=backend,
        )

    async def wait_complete(self, app, count):
        while len(app._cognition_backend.requests) < count:
            await asyncio.sleep(0)
        while app.attention.status().state == "in_flight":
            await asyncio.sleep(0)

    async def test_platform_transitions_wake_with_generic_ordered_facts(self):
        backend = RecordingCognition()
        app = self.make_app(backend)
        await app.start()
        app.set_goal("notice platform transitions")
        for count, event in enumerate((thermal(), thermal(False), memory(), memory(False)), 1):
            await app.events.publish(event)
            await self.wait_complete(app, count)
        self.assertEqual(
            [app_request[1].split("Semantic observation:\n", 1)[1].splitlines()[0]
             for app_request in backend.requests],
            ["  kind: thermal_warning_raised", "  kind: thermal_warning_cleared",
             "  kind: memory_pressure_raised", "  kind: memory_pressure_cleared"],
        )
        self.assertIn("condition: thermal\n  transition: raised", backend.requests[0][1])
        self.assertIn("source: platform_monitor", backend.requests[0][1])
        self.assertIn("cpu_temp_c: 30.0", backend.requests[0][1])
        self.assertEqual(app.working_memory.snapshot(), ())
        await app.stop()

    async def test_disabled_and_presence_do_not_wake(self):
        backend = RecordingCognition()
        app = self.make_app(backend, platform_attention=False)
        await app.start()
        app.set_goal("opaque")
        await app.events.publish(thermal())
        await app.events.publish(PresenceChanged(
            source="test", previous_present=False, present=True
        ))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(backend.requests, [])
        await app.stop()

    async def test_thermal_warning_from_non_monitor_source_does_not_wake(self):
        backend = RecordingCognition()
        app = self.make_app(backend)
        await app.start()
        app.set_goal("opaque")
        for source in ("test", "platform_monitor:other", "reflex:thermal"):
            with self.subTest(source=source):
                await app.events.publish(ThermalWarningRaised(
                    source=source,
                    cpu_temperature_celsius=81.0,
                    warning_threshold_celsius=80.0,
                ))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertEqual(backend.requests, [])
        await app.stop()

    async def test_different_source_is_suppressed_not_queued(self):
        backend = RecordingCognition(blocked=True)
        app = self.make_app(backend)
        await app.start()
        app.set_goal("opaque")
        await app.events.publish(thermal())
        await backend.started.wait()
        await app.events.publish(memory())
        await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 1)
        backend.release.set()
        await self.wait_complete(app, 1)
        await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 1)
        await app.stop()
