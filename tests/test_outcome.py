import asyncio
import json
import unittest

from embodied_runtime.app import (
    COMPLETE_GOAL_TOOL, OUTCOME_EVALUATION_REQUEST, ApplicationOptions,
    RobotApplication,
)
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cognition import CognitionError, CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


class ScriptedBackend(TextCognitionBackend):
    identifier = "scripted"

    def __init__(self, outcome_call=None, *, fail_after_closure=False):
        self.requests = []
        self.outcome_call = outcome_call
        self.fail_after_closure = fail_after_closure

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tools, tool_executor,
                              refreshed_instructions))
        if len(self.requests) == 1:
            result = await tool_executor(CognitionToolCall(
                "orient_body", '{"yaw_degrees":35,"pitch_degrees":-10}'
            ))
            self.initiative_result = json.loads(result.output)
            self.initiative_refresh = refreshed_instructions()
            return "action considered"
        if self.outcome_call:
            result = await tool_executor(self.outcome_call)
            self.outcome_result = json.loads(result.output)
            self.outcome_refresh = refreshed_instructions()
            if self.fail_after_closure:
                raise CognitionError("final continuation failed")
        return "outcome considered"


class OutcomeTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, backend, *, closure=True):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(
                initiative_enabled=True, initiative_actions_enabled=True,
                initiative_goal_closure_enabled=closure,
            ),
            platform_provider=Platform(), body_backend=VirtualBodyBackend(),
            cognition_backend=backend,
        )

    async def run_episode(self, app):
        await app.start()
        await app.set_body_orientation(yaw_degrees=35, pitch_degrees=-10)
        goal = app.set_goal("opaque goal")
        app.working_memory.append("operator", "history")
        memory = app.working_memory.snapshot()
        await app.set_body_orientation(
            yaw_degrees=0, pitch_degrees=0, source="reflex:test"
        )
        while not app._cognition_backend.requests:
            await asyncio.sleep(0)
        while app.attention.status().state == "in_flight":
            await asyncio.sleep(0)
        return goal, memory

    async def test_maintenance_goal_remains_active_and_outcome_is_independent(self):
        backend = ScriptedBackend()
        app = self.make_app(backend)
        goal, memory = await self.run_episode(app)
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(backend.requests[1][0], OUTCOME_EVALUATION_REQUEST)
        self.assertIs(app.active_goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        names = [tool.name for tool in backend.requests[1][2]]
        self.assertEqual(names, ["complete_goal"])
        self.assertNotIn("orient_body", names)
        self.assertNotIn("set_goal", names)
        self.assertNotIn("resolve_goal", names)
        self.assertIn("yaw_deg: 35.0", backend.requests[1][1])
        self.assertIn("attention_source: reflex:test", backend.requests[1][1])
        self.assertIn("previous_yaw_deg: 35.0", backend.requests[1][1])
        self.assertIn("previous_pitch_deg: -10.0", backend.requests[1][1])
        self.assertIn("source: reflex:test", backend.requests[1][1])
        self.assertNotIn(
            "No semantic tools or actions are available in this pass",
            backend.requests[1][1],
        )
        self.assertIn("operator", backend.requests[1][1])
        self.assertEqual(app.attention.status().last_outcome_state, "completed")
        self.assertEqual(app.attention.status().last_goal_closure, "none")
        await app.stop()

    async def test_terminal_goal_completes_and_refreshes_no_active_goal(self):
        backend = ScriptedBackend(CognitionToolCall("complete_goal", "{}"))
        app = self.make_app(backend)
        _, memory = await self.run_episode(app)
        self.assertIsNone(app.active_goal)
        self.assertEqual(backend.outcome_result, {"status": "completed"})
        self.assertIn("Active goal", backend.outcome_refresh)
        self.assertIn("  state: none", backend.outcome_refresh)
        self.assertEqual(app.working_memory.snapshot(), memory)
        self.assertEqual(app.attention.status().last_goal_closure, "completed")
        await app.stop()

    async def test_phase_seven_without_flag_has_no_outcome_request(self):
        backend = ScriptedBackend()
        app = self.make_app(backend, closure=False)
        goal, memory = await self.run_episode(app)
        self.assertEqual(len(backend.requests), 1)
        self.assertIs(app.active_goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        await app.stop()

    async def test_no_action_has_no_outcome_request(self):
        class NoAction(ScriptedBackend):
            async def respond(self, message, **kwargs):
                self.requests.append((message, kwargs))
                return "no action"
        backend = NoAction()
        app = self.make_app(backend)
        await app.start()
        app.set_goal("opaque")
        await app.set_body_orientation(yaw_degrees=1, pitch_degrees=0,
                                       source="reflex:test")
        while not backend.requests:
            await asyncio.sleep(0)
        while app.attention.status().state == "in_flight":
            await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(app.attention.status().last_outcome_state, "not_run")
        await app.stop()

    async def test_rejected_action_evaluates_read_only(self):
        class Rejected(ScriptedBackend):
            async def respond(self, message, *, instructions=None, tools=(),
                              tool_executor=None, refreshed_instructions=None):
                self.requests.append((message, instructions, tools))
                if len(self.requests) == 1:
                    await tool_executor(CognitionToolCall(
                        "orient_body", '{"yaw_degrees":500,"pitch_degrees":0}'
                    ))
                return "done"
        backend = Rejected()
        app = self.make_app(backend)
        goal, _ = await self.run_episode(app)
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(backend.requests[1][2], ())
        self.assertIs(app.active_goal, goal)
        await app.stop()

    async def test_stale_goal_completion_is_rejected(self):
        app = self.make_app(ScriptedBackend())
        await app.start()
        old = app.set_goal("old")
        app.clear_goal()
        newer = app.set_goal("new")
        result = app._execute_outcome_tool(
            CognitionToolCall("complete_goal", "{}"), old, "applied"
        )
        self.assertEqual(json.loads(result.output)["status"], "rejected")
        self.assertIs(app.active_goal, newer)
        await app.stop()

    async def test_completion_survives_final_provider_failure(self):
        backend = ScriptedBackend(
            CognitionToolCall("complete_goal", "{}"), fail_after_closure=True
        )
        app = self.make_app(backend)
        _, memory = await self.run_episode(app)
        self.assertIsNone(app.active_goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        self.assertEqual(app.attention.status().last_outcome_state, "failed")
        self.assertEqual(app.attention.status().last_goal_closure, "completed")
        await app.stop()

    async def test_provider_failure_before_completion_preserves_goal(self):
        class FailingOutcome(ScriptedBackend):
            async def respond(self, message, **kwargs):
                if message == OUTCOME_EVALUATION_REQUEST:
                    self.requests.append((message, kwargs))
                    raise CognitionError("outcome unavailable")
                return await super().respond(message, **kwargs)
        backend = FailingOutcome()
        app = self.make_app(backend)
        goal, memory = await self.run_episode(app)
        self.assertIs(app.active_goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        self.assertEqual(app.attention.status().last_outcome_state, "failed")
        self.assertEqual(app.attention.status().last_goal_closure, "none")
        await app.stop()

    async def test_stop_cancels_blocking_outcome_as_same_initiative_task(self):
        class BlockingOutcome(ScriptedBackend):
            def __init__(self):
                super().__init__()
                self.outcome_started = asyncio.Event()

            async def respond(self, message, **kwargs):
                if message == OUTCOME_EVALUATION_REQUEST:
                    self.requests.append((message, kwargs))
                    self.outcome_started.set()
                    await asyncio.Event().wait()
                return await super().respond(message, **kwargs)
        backend = BlockingOutcome()
        app = self.make_app(backend)
        await app.start()
        app.set_goal("goal")
        await app.set_body_orientation(yaw_degrees=1, pitch_degrees=0,
                                       source="reflex:test")
        await asyncio.wait_for(backend.outcome_started.wait(), 1)
        task = app.attention._task
        await asyncio.wait_for(app.stop(), 1)
        self.assertTrue(task.cancelled())

    async def test_outcome_tool_schema_and_unknown_tool_rejection(self):
        self.assertEqual(COMPLETE_GOAL_TOOL.parameters, {
            "type": "object", "properties": {}, "required": [],
            "additionalProperties": False,
        })
        app = self.make_app(ScriptedBackend())
        await app.start()
        goal = app.set_goal("goal")
        result = app._execute_outcome_tool(
            CognitionToolCall("resolve_goal", "{}"), goal, "applied"
        )
        self.assertEqual(json.loads(result.output)["status"], "rejected")
        self.assertIs(app.active_goal, goal)
        await app.stop()

    async def test_completion_executor_revalidates_actual_action_status(self):
        app = self.make_app(ScriptedBackend())
        await app.start()
        goal = app.set_goal("goal")
        result = app._execute_outcome_tool(
            CognitionToolCall("complete_goal", "{}"), goal, "rejected"
        )
        self.assertEqual(json.loads(result.output)["status"], "rejected")
        self.assertIs(app.active_goal, goal)
        await app.stop()
