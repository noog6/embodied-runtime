import asyncio
import json
import unittest
from unittest.mock import patch

from embodied_runtime.app import (
    INSPECTION_FOLLOWUP_REQUEST, OUTCOME_EVALUATION_REQUEST, ApplicationOptions,
    INSPECT_SELF_TOOL, RobotApplication,
)
from embodied_runtime.attention import (
    ACTION_INITIATIVE_REQUEST, CONTINUATION_INITIATIVE_REQUEST, AttentionStimulus,
)
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cognition import CognitionError, CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.events import BodyOrientationChanged
from embodied_runtime.inspection import HostSelfInspector, SelfInspectionFact, SelfInspectionResult
from embodied_runtime.interaction import OperatorMessageSink
from embodied_runtime.profile import RobotProfile
from embodied_runtime.state import LifecycleState
from tests.test_platform import snapshot


class StaticPlatform:
    def snapshot(self):
        return snapshot()


class FakeInspector:
    def __init__(self):
        self.areas = []

    def inspect(self, area):
        self.areas.append(area)
        return SelfInspectionResult(area, (SelfInspectionFact("sample", "bounded"),))


class RecordingSink(OperatorMessageSink):
    def __init__(self):
        self.messages = []

    async def deliver(self, message):
        self.messages.append(message)


class SequenceBackend(TextCognitionBackend):
    identifier = "inspection-sequence"

    def __init__(self, handlers):
        self.handlers = list(handlers)
        self.requests = []
        self.results = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tuple(t.name for t in tools)))
        handler = self.handlers.pop(0)
        return await handler(self, tool_executor)


async def call(call):
    async def handler(backend, executor):
        result = await executor(call)
        backend.results.append(json.loads(result.output))
        return "done"
    return handler


async def no_call(backend, executor):
    return "no effect"


class InspectionTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, inspector=None):
        return RobotApplication(
            RobotProfile("test", "Test", "test"), VirtualHardwareBackend(),
            platform_provider=StaticPlatform(), self_inspector=inspector,
        )

    def test_exact_schema(self):
        self.assertEqual(INSPECT_SELF_TOOL.parameters, {
            "type": "object", "properties": {"area": {"type": "string", "enum": [
                "network", "storage", "camera", "runtime",
            ]}}, "required": ["area"], "additionalProperties": False,
        })

    async def test_exact_validation_and_read_only(self):
        fake = FakeInspector()
        app = self.make_app(fake)
        await app.start()
        before = (app.runtime_state, app.active_goal, app.working_memory.snapshot())
        for arguments in ("{}", '{"area":"disk"}', '{"area":["storage"]}',
                          '{"area":"storage","path":"/home"}',
                          '{"area":"network","command":"ip addr"}'):
            result, fact = app._execute_self_inspection(
                CognitionToolCall("inspect_self", arguments)
            )
            self.assertEqual(json.loads(result.output)["status"], "rejected")
            self.assertIsNone(fact)
        result, fact = app._execute_self_inspection(
            CognitionToolCall("inspect_self", '{"area":"storage"}')
        )
        self.assertEqual(json.loads(result.output)["status"], "applied")
        self.assertEqual(fact.area, "storage")
        self.assertEqual(fake.areas, ["storage"])
        self.assertEqual(before, (app.runtime_state, app.active_goal,
                                  app.working_memory.snapshot()))
        await app.stop()

    def make_initiative_app(self, backend, *, inspector=None, sink=None,
                            continuation=True, closure=True):
        return RobotApplication(
            RobotProfile("test", "Test", "test"), VirtualHardwareBackend(),
            ApplicationOptions(
                initiative_enabled=True, initiative_actions_enabled=True,
                initiative_messages_enabled=True,
                initiative_continuation_enabled=continuation,
                initiative_goal_closure_enabled=closure,
            ), platform_provider=StaticPlatform(), body_backend=VirtualBodyBackend(),
            cognition_backend=backend, operator_message_sink=sink or RecordingSink(),
            self_inspector=inspector,
        )

    async def test_inspection_only_has_no_action_diagnostics_or_outcome(self):
        fake = FakeInspector()
        backend = SequenceBackend([
            await call(CognitionToolCall("inspect_self", '{"area":"storage"}')),
            no_call,
        ])
        app = self.make_initiative_app(backend, inspector=fake)
        await app.start()
        goal = app.set_goal("Observe storage and remain active")
        memory = app.working_memory.snapshot()
        outcome = await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 1, 0, 0
        ))
        status = app.attention.status()
        self.assertEqual((outcome.action, outcome.action_status), (None, None))
        self.assertEqual((status.last_action, status.last_action_status), (None, None))
        self.assertEqual((status.last_inspection_state, status.last_inspection_area,
                          status.last_inspection_status),
                         ("completed", "storage", "applied"))
        self.assertEqual(fake.areas, ["storage"])
        self.assertEqual(len(backend.requests), 2)
        self.assertNotIn(OUTCOME_EVALUATION_REQUEST, [r[0] for r in backend.requests])
        self.assertIs(app.active_goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        await app.stop()

    async def test_request_a_enforces_one_inspection_budget(self):
        fake = FakeInspector()

        async def malicious(backend, executor):
            for area in ("storage", "network"):
                result = await executor(CognitionToolCall(
                    "inspect_self", json.dumps({"area": area})
                ))
                backend.results.append(json.loads(result.output))
            return "done"

        backend = SequenceBackend([malicious, no_call])
        app = self.make_initiative_app(backend, inspector=fake)
        await app.start()
        app.set_goal("Inspect once")
        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 1, 0, 0
        ))
        self.assertEqual(fake.areas, ["storage"])
        self.assertEqual([r["status"] for r in backend.results], ["applied", "rejected"])
        await app.stop()

    async def test_failed_inspection_stops_without_followup(self):
        class FailingInspector(FakeInspector):
            def inspect(self, area):
                self.areas.append(area)
                raise OSError("unavailable")

        fake = FailingInspector()
        backend = SequenceBackend([
            await call(CognitionToolCall("inspect_self", '{"area":"storage"}')),
        ])
        app = self.make_initiative_app(backend, inspector=fake)
        await app.start()
        goal = app.set_goal("Inspect storage")
        memory = app.working_memory.snapshot()
        outcome = await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 1, 0, 0
        ))
        self.assertEqual((outcome.action, outcome.action_status), (None, None))
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(fake.areas, ["storage"])
        status = app.attention.status()
        self.assertEqual((status.last_inspection_state, status.last_inspection_status),
                         ("failed", "rejected"))
        self.assertIs(app.active_goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        await app.stop()

    async def test_followup_failure_preserves_effect_and_stops_sequence(self):
        sink = RecordingSink()

        async def applied_then_failure(backend, executor):
            result = await executor(CognitionToolCall(
                "address_operator", '{"message":"Runtime inspected."}'
            ))
            backend.results.append(json.loads(result.output))
            raise CognitionError("finalization failed")

        backend = SequenceBackend([
            await call(CognitionToolCall("inspect_self", '{"area":"runtime"}')),
            applied_then_failure,
        ])
        app = self.make_initiative_app(backend, sink=sink)
        await app.start()
        goal = app.set_goal("Ongoing maintenance")
        outcome = await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 1, 0, 0
        ))
        self.assertEqual((outcome.action, outcome.action_status),
                         ("address_operator", "applied"))
        self.assertEqual(len(sink.messages), 1)
        self.assertEqual([r[0] for r in backend.requests],
                         [ACTION_INITIATIVE_REQUEST, INSPECTION_FOLLOWUP_REQUEST])
        self.assertEqual((app.attention.status().last_action,
                          app.attention.status().last_action_status),
                         ("address_operator", "applied"))
        self.assertIs(app.active_goal, goal)
        self.assertIs(app.state, LifecycleState.RUNNING)
        await app.stop()

    async def test_compound_inspection_two_effects_and_outcome_stop(self):
        sink = RecordingSink()
        fake = FakeInspector()
        captured_outcome = []

        async def outcome(backend, executor):
            captured_outcome.append(backend.requests[-1][1])
            return "keep active"

        backend = SequenceBackend([
            await call(CognitionToolCall("inspect_self", '{"area":"runtime"}')),
            await call(CognitionToolCall(
                "address_operator", '{"message":"The body moved."}'
            )),
            await call(CognitionToolCall(
                "orient_body", '{"yaw_degrees":45,"pitch_degrees":-20}'
            )),
            outcome,
        ])
        app = self.make_initiative_app(backend, sink=sink, inspector=fake)
        await app.start()
        goal = app.set_goal("Keep the body at 45/-20 as ongoing maintenance")
        memory = app.working_memory.snapshot()
        await app.events.publish(BodyOrientationChanged(
            source="reflex:presence_centering", previous_yaw_degrees=45,
            previous_pitch_degrees=-20, yaw_degrees=0, pitch_degrees=0,
        ))
        for _ in range(100):
            if app.attention.status().state != "in_flight" and len(backend.requests) == 4:
                break
            await asyncio.sleep(0)
        self.assertEqual([r[0] for r in backend.requests], [
            ACTION_INITIATIVE_REQUEST, INSPECTION_FOLLOWUP_REQUEST,
            CONTINUATION_INITIATIVE_REQUEST, OUTCOME_EVALUATION_REQUEST,
        ])
        self.assertIn("effect_1_name: address_operator", captured_outcome[0])
        self.assertIn("effect_2_name: orient_body", captured_outcome[0])
        self.assertNotIn("effect_1_name: inspect_self", captured_outcome[0])
        self.assertEqual(len(sink.messages), 1)
        self.assertEqual(app.attention.status().last_inspection_status, "applied")
        self.assertEqual(fake.areas, [])  # runtime metadata is application-owned
        self.assertEqual((app.runtime_state.body.yaw_degrees,
                          app.runtime_state.body.pitch_degrees), (45, -20))
        self.assertIs(app.active_goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        await app.stop()

    async def test_same_goal_race_rejects_followup_effect(self):
        sink = RecordingSink()
        replacement = []

        async def replace_goal_then_call(backend, executor):
            app.clear_goal()
            replacement.append(app.set_goal("Replacement goal"))
            result = await executor(CognitionToolCall(
                "address_operator", '{"message":"stale"}'
            ))
            backend.results.append(json.loads(result.output))
            return "done"

        backend = SequenceBackend([
            await call(CognitionToolCall("inspect_self", '{"area":"runtime"}')),
            replace_goal_then_call,
        ])
        app = self.make_initiative_app(backend, sink=sink)
        await app.start()
        app.set_goal("Original goal")
        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 1, 0, 0
        ))
        self.assertIs(app.active_goal, replacement[0])
        self.assertEqual(sink.messages, [])
        self.assertEqual(backend.results[-1]["status"], "rejected")
        self.assertEqual(len(backend.requests), 2)
        await app.stop()

    async def test_stopping_race_rejects_followup_effect(self):
        sink = RecordingSink()

        async def stop_then_call(backend, executor):
            app._set_lifecycle(LifecycleState.STOPPING)
            result = await executor(CognitionToolCall(
                "address_operator", '{"message":"stale"}'
            ))
            backend.results.append(json.loads(result.output))
            return "done"

        backend = SequenceBackend([
            await call(CognitionToolCall("inspect_self", '{"area":"runtime"}')),
            stop_then_call,
        ])
        app = self.make_initiative_app(backend, sink=sink)
        await app.start()
        app.set_goal("Original goal")
        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 1, 0, 0
        ))
        self.assertEqual(sink.messages, [])
        self.assertEqual(backend.results[-1]["status"], "rejected")
        self.assertEqual(len(backend.requests), 2)
        await app.stop()

    async def test_direct_effect_does_not_force_inspection(self):
        fake = FakeInspector()
        backend = SequenceBackend([
            await call(CognitionToolCall(
                "orient_body", '{"yaw_degrees":45,"pitch_degrees":-20}'
            )),
            no_call,
            no_call,
        ])
        app = self.make_initiative_app(backend, inspector=fake)
        await app.start()
        app.set_goal("Keep body at 45/-20")
        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 45, -20, 0, 0
        ))
        self.assertEqual(fake.areas, [])
        self.assertEqual(app.attention.status().last_inspection_state, "not_run")
        self.assertEqual((app.runtime_state.body.yaw_degrees,
                          app.runtime_state.body.pitch_degrees), (45, -20))
        await app.stop()

    def test_storage_is_fixed_and_exact(self):
        with patch("embodied_runtime.inspection.shutil.disk_usage") as usage:
            usage.return_value = type("Usage", (), {"total": 100, "used": 60, "free": 40})()
            result = HostSelfInspector().inspect("storage")
        usage.assert_called_once_with("/")
        self.assertEqual([(f.name, f.value) for f in result.facts], [
            ("filesystem", "/"), ("total_bytes", "100"), ("used_bytes", "60"),
            ("free_bytes", "40"), ("free_ratio", "0.4000"),
        ])

    async def test_camera_and_runtime_do_not_expose_content(self):
        app = self.make_app()
        await app.start()
        for area in ("camera", "runtime"):
            _, result = app._execute_self_inspection(
                CognitionToolCall("inspect_self", json.dumps({"area": area}))
            )
            rendered = repr(result)
            self.assertNotIn("API", rendered)
            self.assertLessEqual(len(result.facts), 17)
        await app.stop()
