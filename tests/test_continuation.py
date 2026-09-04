import asyncio
import json
import unittest

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.attention import (
    ACTION_INITIATIVE_REQUEST, CONTINUATION_INITIATIVE_REQUEST, AttentionStimulus,
)
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cognition import CognitionError, CognitionToolCall, TextCognitionBackend
from embodied_runtime.events import ThermalWarningRaised
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.interaction import OperatorMessageSink
from embodied_runtime.profile import RobotProfile
from embodied_runtime.state import BodyState, LifecycleState
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


class Sink(OperatorMessageSink):
    def __init__(self):
        self.messages = []

    async def deliver(self, message):
        self.messages.append(message)


class SequenceBackend(TextCognitionBackend):
    identifier = "sequence"

    def __init__(self, scripts, *, hooks=None, failures=()):
        self.scripts = scripts
        self.hooks = hooks or {}
        self.failures = set(failures)
        self.requests = []
        self.results = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        index = len(self.requests)
        self.requests.append((message, instructions, tools, refreshed_instructions))
        hook = self.hooks.get(index)
        if hook:
            hook()
        for call in self.scripts[index] if index < len(self.scripts) else ():
            self.results.append(json.loads((await tool_executor(call)).output))
        if index in self.failures:
            raise CognitionError("provider final response failed")
        return f"response {index}"


class ContinuationTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, backend, *, closure=False, platform_attention=False):
        sink = Sink()
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(
                initiative_enabled=True,
                initiative_platform_attention_enabled=platform_attention,
                initiative_actions_enabled=True,
                initiative_messages_enabled=True,
                initiative_continuation_enabled=True,
                initiative_goal_closure_enabled=closure,
            ),
            platform_provider=Platform(), body_backend=VirtualBodyBackend(),
            cognition_backend=backend, operator_message_sink=sink,
        )
        return app, sink

    async def wait_for_episode(self, app, backend, request_count):
        while len(backend.requests) < request_count:
            await asyncio.sleep(0)
        while app.attention.status().state == "in_flight":
            await asyncio.sleep(0)

    async def test_message_then_fresh_restore_uses_same_episode_snapshot(self):
        backend = SequenceBackend((
            (CognitionToolCall("address_operator", '{"message":"Nick, body moved."}'),),
            (CognitionToolCall("orient_body", '{"yaw_degrees":45,"pitch_degrees":-20}'),),
        ))
        app, sink = self.make_app(backend)
        await app.start()
        await app.set_body_orientation(yaw_degrees=33, pitch_degrees=-15)
        goal = app.set_goal("notify then restore")
        app.working_memory.append("prior", "history")
        memory = app.working_memory.snapshot()
        await app.set_body_orientation(yaw_degrees=0, pitch_degrees=0, source="reflex:test")
        while app.attention.status().state == "in_flight":
            await __import__("asyncio").sleep(0)
        self.assertEqual([request[0] for request in backend.requests],
                         [ACTION_INITIATIVE_REQUEST, CONTINUATION_INITIATIVE_REQUEST])
        self.assertEqual([tool.name for tool in backend.requests[1][2]], ["schedule_followup", "orient_body"])
        self.assertIn("yaw_deg: 0.0", backend.requests[1][1])
        self.assertIn("first_effect_name: address_operator", backend.requests[1][1])
        self.assertIn("prior", backend.requests[1][1])
        self.assertIn("yaw_deg: 45.0", backend.requests[1][3]())
        self.assertEqual(app.runtime_state.body, BodyState(45.0, -20.0))
        self.assertIs(app.active_goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        self.assertEqual(len(sink.messages), 1)
        status = app.attention.status()
        self.assertEqual((status.last_action, status.last_action_status),
                         ("address_operator", "applied"))
        self.assertEqual((status.last_continuation_state,
                          status.last_continuation_action,
                          status.last_continuation_action_status),
                         ("completed", "orient_body", "applied"))
        await app.stop()

    async def test_platform_thermal_message_can_continue_without_second_effect(self):
        backend = SequenceBackend((
            (CognitionToolCall(
                "address_operator", '{"message":"Platform temperature is elevated."}'
            ),),
            (),
        ))
        app, sink = self.make_app(backend, platform_attention=True)
        await app.start()
        goal = app.set_goal("monitor platform health")
        app.working_memory.append("operator context", "remembered context")
        memory = app.working_memory.snapshot()

        await app.events.publish(ThermalWarningRaised(
            source="platform_monitor",
            cpu_temperature_celsius=81.0,
            warning_threshold_celsius=80.0,
        ))
        await self.wait_for_episode(app, backend, 2)

        self.assertEqual(
            [request[0] for request in backend.requests],
            [ACTION_INITIATIVE_REQUEST, CONTINUATION_INITIATIVE_REQUEST],
        )
        self.assertIn("operator context", backend.requests[0][1])
        self.assertIn("operator context", backend.requests[1][1])
        self.assertEqual([tool.name for tool in backend.requests[1][2]], ["schedule_followup", "orient_body"])
        self.assertIs(app.active_goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        self.assertEqual(len(sink.messages), 1)
        status = app.attention.status()
        self.assertEqual((status.last_action, status.last_action_status),
                         ("address_operator", "applied"))
        self.assertEqual((status.last_continuation_state,
                          status.last_continuation_action,
                          status.last_continuation_action_status),
                         ("completed", None, None))
        await app.stop()

    async def test_reverse_order_and_one_continuation_call_budget(self):
        backend = SequenceBackend((
            (CognitionToolCall("orient_body", '{"yaw_degrees":45,"pitch_degrees":-20}'),),
            (CognitionToolCall("address_operator", '{"message":"restored"}'),
             CognitionToolCall("address_operator", '{"message":"again"}')),
        ))
        app, sink = self.make_app(backend)
        await app.start()
        app.set_goal("restore then notify")
        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 1, 0, 0
        ))
        self.assertEqual([tool.name for tool in backend.requests[1][2]],
                         ["schedule_followup", "address_operator"])
        self.assertEqual([result["status"] for result in backend.results],
                         ["applied", "applied", "rejected"])
        self.assertEqual(len(sink.messages), 1)
        await app.stop()

    async def test_rejected_first_and_no_first_effect_never_continue(self):
        for first in (
            (CognitionToolCall("address_operator", '{"message":""}'),), (),
        ):
            with self.subTest(first=first):
                backend = SequenceBackend((first, (CognitionToolCall(
                    "orient_body", '{"yaw_degrees":45,"pitch_degrees":-20}'
                ),)))
                app, sink = self.make_app(backend)
                await app.start()
                app.set_goal("goal")
                await app._request_initiative(AttentionStimulus(
                    "body_orientation_changed", "reflex:test", 1, 0, 0, 0
                ))
                self.assertEqual(len(backend.requests), 1)
                self.assertEqual(sink.messages, [])
                self.assertEqual(app.runtime_state.body, BodyState(0.0, 0.0))
                await app.stop()

    async def test_same_tool_is_excluded_and_direct_request_rejected(self):
        backend = SequenceBackend((
            (CognitionToolCall("address_operator", '{"message":"first"}'),),
            (CognitionToolCall("address_operator", '{"message":"second"}'),),
        ))
        app, sink = self.make_app(backend)
        await app.start()
        app.set_goal("goal")
        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 0, 0, 0
        ))
        self.assertEqual([tool.name for tool in backend.requests[1][2]], ["schedule_followup", "orient_body"])
        self.assertEqual(backend.results[-1]["status"], "rejected")
        self.assertEqual(len(sink.messages), 1)
        await app.stop()

    async def test_goal_replacement_before_continuation_tool_blocks_effect(self):
        app_holder = {}
        def replace_goal():
            app = app_holder["app"]
            app.clear_goal()
            app.set_goal("replacement")
        backend = SequenceBackend((
            (CognitionToolCall("address_operator", '{"message":"first"}'),),
            (CognitionToolCall("orient_body", '{"yaw_degrees":45,"pitch_degrees":-20}'),),
        ), hooks={1: replace_goal})
        app, sink = self.make_app(backend)
        app_holder["app"] = app
        await app.start()
        original = app.set_goal("original")
        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 0, 0, 0
        ))
        self.assertIsNot(app.active_goal, original)
        self.assertEqual(app.active_goal.description, "replacement")
        self.assertEqual(backend.results[-1]["status"], "rejected")
        self.assertEqual(app.runtime_state.body, BodyState(0.0, 0.0))
        await app.stop()

    async def test_two_effect_outcome_all_applied_controls_completion(self):
        backend = SequenceBackend((
            (CognitionToolCall("address_operator", '{"message":"first"}'),),
            (CognitionToolCall("orient_body", '{"yaw_degrees":45,"pitch_degrees":-20}'),),
            (CognitionToolCall("complete_goal", '{}'),),
        ))
        app, sink = self.make_app(backend, closure=True)
        await app.start()
        app.set_goal("terminal")
        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 0, 0, 0
        ))
        self.assertIsNone(app.active_goal)
        self.assertIn("effect_1_name: address_operator", backend.requests[2][1])
        self.assertIn("effect_2_name: orient_body", backend.requests[2][1])
        self.assertEqual([tool.name for tool in backend.requests[2][2]], ["complete_goal"])
        self.assertEqual(len(sink.messages), 1)
        await app.stop()

    async def test_rejected_second_is_grounded_and_disables_completion(self):
        backend = SequenceBackend((
            (CognitionToolCall("address_operator", '{"message":"first"}'),),
            (CognitionToolCall("orient_body", '{"yaw_degrees":500,"pitch_degrees":0}'),),
            (),
        ))
        app, _ = self.make_app(backend, closure=True)
        await app.start()
        goal = app.set_goal("goal")
        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 0, 0, 0
        ))
        self.assertIs(app.active_goal, goal)
        self.assertEqual(backend.requests[2][2], ())
        self.assertIn("effect_2_status: rejected", backend.requests[2][1])
        await app.stop()

    async def test_second_effect_survives_final_provider_failure_without_outcome(self):
        backend = SequenceBackend((
            (CognitionToolCall("address_operator", '{"message":"first"}'),),
            (CognitionToolCall("orient_body", '{"yaw_degrees":45,"pitch_degrees":-20}'),),
        ), failures={1})
        app, _ = self.make_app(backend, closure=True)
        await app.start()
        goal = app.set_goal("goal")
        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 0, 0, 0
        ))
        self.assertEqual(app.runtime_state.body, BodyState(45.0, -20.0))
        self.assertIs(app.active_goal, goal)
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(app.attention.status().last_continuation_state, "failed")
        await app.stop()

    async def test_repeated_maintenance_restoration_rearms_reflex_attention(self):
        backend = SequenceBackend((
            (CognitionToolCall("address_operator", '{"message":"episode one"}'),),
            (CognitionToolCall("orient_body", '{"yaw_degrees":45,"pitch_degrees":-20}'),),
            (CognitionToolCall("address_operator", '{"message":"episode two"}'),),
            (CognitionToolCall("orient_body", '{"yaw_degrees":45,"pitch_degrees":-20}'),),
        ))
        app, sink = self.make_app(backend)
        await app.start()
        await app.set_body_orientation(yaw_degrees=45, pitch_degrees=-20)
        goal = app.set_goal("Keep 45/-20; notify and restore after every reflex move")
        app.working_memory.append("operator", "maintenance history")
        memory = app.working_memory.snapshot()

        await app.set_body_orientation(
            yaw_degrees=0, pitch_degrees=0, source="reflex:test"
        )
        await self.wait_for_episode(app, backend, 2)
        self.assertEqual(len(sink.messages), 1)
        self.assertEqual(app.runtime_state.body, BodyState(45.0, -20.0))
        self.assertIs(app.active_goal, goal)
        self.assertEqual(len(backend.results), 2)

        # The first continuation restored the body. A new reflex transition from
        # that restored pose must naturally wake a second, independent episode.
        await app.set_body_orientation(
            yaw_degrees=0, pitch_degrees=0, source="reflex:test"
        )
        await self.wait_for_episode(app, backend, 4)

        self.assertEqual(app.attention.status().state, "completed")
        self.assertEqual(app.attention.status().last_trigger,
                         "body_orientation_changed")
        self.assertEqual(
            [request[0] for request in backend.requests],
            [ACTION_INITIATIVE_REQUEST, CONTINUATION_INITIATIVE_REQUEST] * 2,
        )
        self.assertEqual([result["status"] for result in backend.results],
                         ["applied"] * 4)
        self.assertEqual(len(sink.messages), 2)
        self.assertEqual(app.runtime_state.body, BodyState(45.0, -20.0))
        self.assertIs(app.active_goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        # Exactly four requests proves initiative-sourced restorations did not
        # recursively wake attention between the two reflex-sourced episodes.
        await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 4)
        await app.stop()

    async def test_stopping_runtime_rejects_continuation_before_second_effect(self):
        app_holder = {}

        def begin_stopping():
            app_holder["app"]._set_lifecycle(LifecycleState.STOPPING)

        backend = SequenceBackend((
            (CognitionToolCall("address_operator", '{"message":"first applied"}'),),
            (CognitionToolCall("orient_body", '{"yaw_degrees":45,"pitch_degrees":-20}'),),
        ), hooks={1: begin_stopping})
        app, sink = self.make_app(backend)
        app_holder["app"] = app
        await app.start()
        goal = app.set_goal("notify then restore")

        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 0, 0, 0
        ))

        self.assertEqual(app.state, LifecycleState.STOPPING)
        self.assertEqual([result["status"] for result in backend.results],
                         ["applied", "rejected"])
        self.assertEqual(len(sink.messages), 1)
        self.assertEqual(app.runtime_state.body, BodyState(0.0, 0.0))
        self.assertIs(app.active_goal, goal)
        self.assertEqual(len(backend.requests), 2)
        await app.stop()
