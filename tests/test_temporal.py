import asyncio
import json
import unittest

from embodied_runtime.app import (
    SCHEDULE_FOLLOWUP_TOOL,
    ApplicationOptions, RobotApplication,
)
from embodied_runtime.attention import AttentionStimulus
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.console import RuntimeConsole
from embodied_runtime.events import EventBus, TemporalFollowupDue
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.interaction import OperatorMessage
from embodied_runtime.observations import (
    SemanticObservation, observation_from_temporal_followup,
)
from embodied_runtime.perception import VisualPerceptionResult
from embodied_runtime.profile import RobotProfile
from embodied_runtime.sensing.camera import CameraFrame
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


class HoldingEventBus(EventBus):
    """Hold a due event after publication but before attention consumes it."""

    def __init__(self):
        super().__init__()
        self.temporal_events = []

    async def publish(self, event):
        if isinstance(event, TemporalFollowupDue):
            self.temporal_events.append(event)
            return
        await super().publish(event)


class FakeTimer:
    def __init__(self):
        self.now = 100.0
        self.waiters = []

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((delay, future))
        await future

    async def advance(self):
        await asyncio.sleep(0)
        delay, future = self.waiters.pop(0)
        self.now += delay
        future.set_result(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)


class Sink:
    def __init__(self): self.messages = []
    async def deliver(self, message: OperatorMessage): self.messages.append(message)


class Camera:
    identifier = "fake-camera"
    is_physical = False

    def __init__(self): self.running = False; self.captures = 0
    @property
    def is_running(self): return self.running
    def start(self): self.running = True
    def stop(self): self.running = False
    def capture_frame(self):
        self.captures += 1
        return CameraFrame(b"jpeg", "image/jpeg", 1, 1, 1)


class Vision:
    identifier = "fake-vision"

    def __init__(self): self.calls = []
    async def interpret(self, frame, focus):
        self.calls.append((frame, focus))
        return VisualPerceptionResult(focus, "A bounded scene.")


class Backend(TextCognitionBackend):
    identifier = "temporal-fake"

    def __init__(self, calls=(), *, fail_after_tool=False, blocked=False):
        self.calls = list(calls)
        self.requests = []
        self.fail_after_tool = fail_after_tool
        self.blocked = blocked
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tools))
        self.started.set()
        if self.blocked:
            await self.release.wait()
        if self.calls:
            result = await tool_executor(self.calls.pop(0))
            if self.fail_after_tool:
                raise RuntimeError("finalization failed")
            refreshed_instructions()
            return json.loads(result.output).get("status", "done")
        return "no action"


class FailingAfterReleaseBackend(Backend):
    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tools))
        self.started.set()
        if len(self.requests) == 1:
            await self.release.wait()
            raise RuntimeError("provider failed")
        return "no action"


class TemporalTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, *, enabled=True, backend=None, sink=None, continuation=False,
                 closure=False, events=None, camera=None, vision=None):
        self.timer = FakeTimer()
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(
                initiative_enabled=enabled, initiative_messages_enabled=sink is not None,
                initiative_continuation_enabled=continuation,
                initiative_goal_closure_enabled=closure,
            ), events=events, platform_provider=Platform(), cognition_backend=backend,
            operator_message_sink=sink, temporal_sleep=self.timer.sleep,
            monotonic_clock=lambda: self.timer.now,
            camera_backend=camera, visual_perception_backend=vision,
        )

    async def schedule(self, app, arguments='{"delay_seconds": 30, "purpose": "revisit goal"}'):
        return await app._execute_initiative_tool(
            CognitionToolCall("schedule_followup", arguments)
        )

    async def test_exact_schema_and_projection_requirements(self):
        self.assertEqual(SCHEDULE_FOLLOWUP_TOOL.parameters, {
            "type": "object",
            "properties": {
                "delay_seconds": {"type": "integer", "minimum": 10, "maximum": 86400},
                "purpose": {"type": "string", "minLength": 1, "maxLength": 300},
            },
            "required": ["delay_seconds", "purpose"],
            "additionalProperties": False,
        })
        app = self.make_app(enabled=True)
        self.assertNotIn(SCHEDULE_FOLLOWUP_TOOL, app.effect_tools())
        await app.start()
        self.assertNotIn(SCHEDULE_FOLLOWUP_TOOL, app.effect_tools())
        app.set_goal("goal")
        self.assertIn(SCHEDULE_FOLLOWUP_TOOL, app.effect_tools())
        await self.schedule(app)
        self.assertNotIn(SCHEDULE_FOLLOWUP_TOOL, app.effect_tools())
        await app.stop()
        disabled = self.make_app(enabled=False)
        await disabled.start(); disabled.set_goal("goal")
        self.assertNotIn(SCHEDULE_FOLLOWUP_TOOL, disabled.effect_tools())
        await disabled.stop()

    async def test_invalid_arguments_do_not_schedule(self):
        invalid = [
            '{"delay_seconds": true, "purpose": "x"}',
            '{"delay_seconds": 10.5, "purpose": "x"}',
            '{"delay_seconds": "10", "purpose": "x"}',
            '{"delay_seconds": 9, "purpose": "x"}',
            '{"delay_seconds": 86401, "purpose": "x"}',
            '{"delay_seconds": 10, "purpose": "   "}',
            json.dumps({"delay_seconds": 10, "purpose": "x" * 301}),
            json.dumps({"delay_seconds": 10, "purpose": "bad\u0000text"}),
            '{"delay_seconds": 10, "purpose": "x", "extra": 1}',
        ]
        app = self.make_app(); await app.start(); app.set_goal("goal")
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                result = await self.schedule(app, arguments)
                self.assertEqual(json.loads(result.output)["status"], "rejected")
                self.assertIsNone(app.temporal.pending)
        await app.stop()

    async def test_schedule_is_one_effect_and_changes_neither_state_nor_memory(self):
        app = self.make_app(); await app.start(); goal = app.set_goal("goal")
        app.working_memory.append("old", "turn")
        state, memory = app.runtime_state, app.working_memory.snapshot()
        result = await self.schedule(app, '{"delay_seconds": 30, "purpose": "  revisit  "}')
        self.assertEqual(json.loads(result.output)["status"], "applied")
        self.assertEqual(app.temporal.pending.purpose, "revisit")
        self.assertIs(app.temporal.pending.goal, goal)
        self.assertIs(app.runtime_state, state)
        self.assertEqual(app.working_memory.snapshot(), memory)
        self.assertEqual(app.attention.status().last_trigger, None)
        original = app.temporal.pending
        rejected = await self.schedule(app, '{"delay_seconds": 40, "purpose": "other"}')
        self.assertEqual(json.loads(rejected.output)["status"], "rejected")
        self.assertIs(app.temporal.pending, original)
        await app.stop()

    async def test_due_emits_once_clears_and_maps_to_fresh_attention(self):
        backend = Backend()
        app = self.make_app(backend=backend)
        events = []
        app.events.subscribe(TemporalFollowupDue, lambda event: self._record(events, event))
        await app.start(); goal = app.set_goal("goal")
        app.working_memory.append("history", "unchanged")
        memory = app.working_memory.snapshot()
        await self.schedule(app)
        app._replace_platform_state(snapshot(hostname="fresh-runtime"))
        await self.timer.advance()
        await backend.started.wait()
        while app.attention.status().state == "in_flight": await asyncio.sleep(0)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].source, events[0].purpose, events[0].delay_seconds),
                         ("temporal_followup", "revisit goal", 30))
        self.assertIsNone(app.temporal.pending)
        self.assertIs(app.active_goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        self.assertEqual(len(backend.requests), 1)
        self.assertIn("temporal_followup_due", backend.requests[0][1])
        self.assertIn("did not reserve future authority", backend.requests[0][1])
        await asyncio.sleep(0)
        self.assertEqual(len(events), 1)
        await app.stop()

    async def test_consumption_rechecks_exact_goal_after_due_publication(self):
        events = HoldingEventBus()
        backend = Backend()
        app = self.make_app(backend=backend, events=events)
        await app.start()
        goal_a = app.set_goal("Goal A")
        await self.schedule(app)
        await self.timer.advance()
        self.assertEqual(len(events.temporal_events), 1)
        due = events.temporal_events[0]
        self.assertIs(due.bound_goal, goal_a)
        self.assertEqual(
            [fact.name for fact in observation_from_temporal_followup(due).facts],
            ["purpose", "delay_seconds"],
        )
        app.clear_goal()
        goal_b = app.set_goal("Goal B")
        await app.attention._on_temporal_event(due)
        await asyncio.sleep(0)
        self.assertEqual(backend.requests, [])
        self.assertIs(app.active_goal, goal_b)
        await app.stop()

    async def test_clear_prevents_already_queued_due_event_from_running(self):
        events = HoldingEventBus()
        backend = Backend()
        app = self.make_app(backend=backend, events=events)
        await app.start(); app.set_goal("Goal A"); await self.schedule(app)
        await self.timer.advance()
        self.assertEqual(app.temporal_followup_status().state, "due_pending")
        due = events.temporal_events[0]
        self.assertTrue(app.clear_temporal_followup())
        self.assertEqual(app.temporal_followup_status().state, "none")
        await app.attention._on_temporal_event(due)
        await asyncio.sleep(0)
        self.assertEqual(backend.requests, [])
        await app.stop()

    async def test_temporal_event_rejects_non_runtime_source(self):
        backend = Backend()
        app = self.make_app(backend=backend)
        await app.start(); goal = app.set_goal("Goal")
        await app.events.publish(TemporalFollowupDue(
            source="forged", purpose="not authorized", delay_seconds=30,
            bound_goal=goal,
        ))
        await asyncio.sleep(0); await asyncio.sleep(0)
        self.assertEqual(backend.requests, [])
        self.assertIs(app.active_goal, goal)
        await app.stop()

    async def test_task_start_rechecks_goal_after_temporal_acceptance(self):
        events = HoldingEventBus()
        backend = Backend([CognitionToolCall(
            "schedule_followup", '{"delay_seconds":30,"purpose":"must not run"}'
        )])
        app = self.make_app(backend=backend, events=events)
        await app.start()
        goal_a = app.set_goal("Goal A")
        await self.schedule(
            app, '{"delay_seconds":30,"purpose":"Goal A purpose"}'
        )
        await self.timer.advance()
        due = events.temporal_events[0]

        # _on_temporal_event accepts and creates the task without yielding to it.
        await app.attention._on_temporal_event(due)
        self.assertEqual(app.attention.status().state, "in_flight")
        app.clear_goal()
        goal_b = app.set_goal("Goal B")
        await asyncio.sleep(0)

        self.assertEqual(backend.requests, [])
        self.assertIs(app.active_goal, goal_b)
        self.assertIsNone(app.temporal.pending)
        self.assertEqual(app.attention.status().state, "idle")
        await app.stop()

    async def test_goal_closure_base_policy_projects_only_schedule_effect(self):
        app = self.make_app(enabled=True, closure=True)
        await app.start(); app.set_goal("Goal")
        self.assertEqual(
            [tool.name for tool in app.initiative_tools()],
            ["inspect_self", "schedule_followup"],
        )
        self.assertEqual(
            [tool.name for tool in app.effect_tools()], ["schedule_followup"]
        )
        await app.stop()

    @staticmethod
    async def _record(target, event):
        target.append(event)

    async def test_goal_change_clear_and_shutdown_cancel_without_due(self):
        for operation in ("clear", "resolve", "shutdown"):
            with self.subTest(operation=operation):
                app = self.make_app(); events = []
                app.events.subscribe(TemporalFollowupDue, lambda e: self._record(events, e))
                await app.start(); app.set_goal("goal"); await self.schedule(app)
                if operation == "clear": app.clear_goal()
                elif operation == "resolve": app.resolve_goal("completed")
                else: await app.stop()
                self.assertIsNone(app.temporal.pending)
                await asyncio.sleep(0)
                if self.timer.waiters:
                    await self.timer.advance()
                self.assertEqual(events, [])
                if operation != "shutdown": await app.stop()

    async def test_console_clear_diagnostics_and_self_inspection_fact(self):
        app = self.make_app(); await app.start(); app.set_goal("goal")
        console = RuntimeConsole(app)
        self.assertEqual(console.execute("followup")[0], "Temporal follow-up\n  state:         none")
        await self.schedule(app)
        report = console.execute("followup")[0]
        self.assertIn("state:         pending", report)
        self.assertIn("delay_seconds: 30", report)
        self.assertIn("remaining_s:   30", report)
        self.assertIn("purpose:       revisit goal", report)
        self.assertNotIn("task", report.lower())
        result, _ = app._execute_self_inspection(CognitionToolCall("inspect_self", '{"area":"runtime"}'))
        facts = {fact["name"]: fact["value"] for fact in json.loads(result.output)["facts"]}
        self.assertEqual(facts["temporal_followup_pending"], "true")
        self.assertIn("cleared:       true", console.execute("followup clear")[0])
        self.assertIsNone(app.temporal.pending)
        await app.stop()

    async def test_scheduling_effect_can_continue_and_counts_toward_two(self):
        sink = Sink()
        backend = Backend([
            CognitionToolCall("schedule_followup", '{"delay_seconds":30,"purpose":"later"}'),
            CognitionToolCall("address_operator", '{"message":"now"}'),
        ])
        app = self.make_app(backend=backend, sink=sink, continuation=True)
        await app.start(); app.set_goal("goal")
        await app._request_initiative(AttentionStimulus(
            SemanticObservation("test", "test", ())
        ))
        self.assertIsNotNone(app.temporal.pending)
        self.assertEqual(len(sink.messages), 1)
        self.assertEqual(len(backend.requests), 2)
        self.assertNotIn("schedule_followup", [t.name for t in backend.requests[1][2]])
        await app.stop()

    async def test_post_inspection_can_schedule_as_first_effect(self):
        backend = Backend([
            CognitionToolCall("inspect_self", '{"area":"runtime"}'),
            CognitionToolCall(
                "schedule_followup", '{"delay_seconds":30,"purpose":"after inspection"}'
            ),
        ])
        app = self.make_app(backend=backend)
        await app.start(); goal = app.set_goal("Maintenance")
        app.working_memory.append("operator", "history")
        memory = app.working_memory.snapshot()
        outcome = await app._request_initiative(AttentionStimulus(
            SemanticObservation("test", "test", ())
        ))
        self.assertEqual((outcome.action, outcome.action_status),
                         ("schedule_followup", "applied"))
        self.assertEqual(len(backend.requests), 2)
        self.assertNotIn("inspect_self", [tool.name for tool in backend.requests[1][2]])
        self.assertIs(app.temporal.pending.goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        self.assertEqual(app.attention.status().last_inspection_status, "applied")
        await app.stop()

    async def test_post_visual_can_schedule_as_first_effect(self):
        camera, vision = Camera(), Vision()
        backend = Backend([
            CognitionToolCall("observe_scene", '{"focus":"current scene"}'),
            CognitionToolCall(
                "schedule_followup", '{"delay_seconds":30,"purpose":"after vision"}'
            ),
        ])
        app = self.make_app(backend=backend, camera=camera, vision=vision)
        await app.start(); goal = app.set_goal("Visual maintenance")
        app.working_memory.append("operator", "history")
        memory = app.working_memory.snapshot()
        outcome = await app._request_initiative(AttentionStimulus(
            SemanticObservation("test", "test", ())
        ))
        self.assertEqual((camera.captures, len(vision.calls)), (1, 1))
        self.assertEqual((outcome.action, outcome.action_status),
                         ("schedule_followup", "applied"))
        self.assertEqual(len(backend.requests), 2)
        self.assertNotIn("observe_scene", [tool.name for tool in backend.requests[1][2]])
        self.assertIs(app.temporal.pending.goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        await app.stop()

    async def test_central_due_episode_uses_fresh_grounding_and_delivers_once(self):
        sink = Sink()
        backend = Backend([
            CognitionToolCall(
                "schedule_followup",
                '{"delay_seconds":30,"purpose":"Tell Nick that the revisit became due."}',
            ),
            CognitionToolCall(
                "address_operator", '{"message":"Temporal follow-up fired."}'
            ),
        ])
        app = self.make_app(backend=backend, sink=sink)
        due_events = []
        app.events.subscribe(TemporalFollowupDue, lambda event: self._record(due_events, event))
        await app.start(); goal = app.set_goal("Ongoing maintenance")
        app.working_memory.append("before schedule", "old snapshot")
        await app._request_initiative(AttentionStimulus(
            SemanticObservation("initial", "test", ())
        ))
        self.assertIs(app.temporal.pending.goal, goal)
        self.assertEqual(len(backend.requests), 1)
        app.working_memory.append("after schedule", "fresh snapshot")
        memory = app.working_memory.snapshot()
        app._replace_platform_state(snapshot(hostname="due-time-host"))
        await self.timer.advance()
        for _ in range(100):
            if len(backend.requests) == 2 and app.attention.status().state != "in_flight":
                break
            await asyncio.sleep(0)
        self.assertEqual(len(due_events), 1)
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(len(sink.messages), 1)
        self.assertEqual(sink.messages[0].text, "Temporal follow-up fired.")
        self.assertIsNone(app.temporal.pending)
        self.assertIs(app.active_goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        initial_instructions = backend.requests[0][1]
        due_instructions = backend.requests[1][1]
        self.assertNotIn("after schedule", initial_instructions)
        self.assertIn("after schedule", due_instructions)
        self.assertIn("due-time-host", due_instructions)
        self.assertEqual(backend.calls, [])
        await asyncio.sleep(0)
        self.assertEqual((len(due_events), len(backend.requests), len(sink.messages)),
                         (1, 2, 1))
        await app.stop()

    async def test_finalization_failure_preserves_authoritative_schedule(self):
        backend = Backend([CognitionToolCall(
            "schedule_followup", '{"delay_seconds":30,"purpose":"later"}'
        )], fail_after_tool=True)
        app = self.make_app(backend=backend, continuation=True)
        await app.start(); app.set_goal("goal")
        with self.assertRaises(RuntimeError):
            await app._request_initiative(AttentionStimulus(SemanticObservation("x", "x", ())))
        self.assertIsNotNone(app.temporal.pending)
        self.assertEqual(app.attention.status().last_action, "schedule_followup")
        self.assertEqual(app.attention.status().last_action_status, "applied")
        self.assertEqual(len(backend.requests), 1)
        await app.stop()

    async def test_due_episode_can_explicitly_reschedule(self):
        backend = Backend([CognitionToolCall(
            "schedule_followup", '{"delay_seconds":45,"purpose":"again"}'
        )])
        app = self.make_app(backend=backend)
        await app.start(); goal = app.set_goal("goal"); await self.schedule(app)
        await self.timer.advance(); await backend.started.wait()
        while app.attention.status().state == "in_flight": await asyncio.sleep(0)
        self.assertIsNotNone(app.temporal.pending)
        self.assertEqual(app.temporal.pending.delay_seconds, 45)
        self.assertIs(app.temporal.pending.goal, goal)
        await app.stop()

    async def test_due_during_attention_is_deferred_once(self):
        backend = Backend(blocked=True)
        app = self.make_app(backend=backend)
        await app.start(); app.set_goal("goal")
        from embodied_runtime.events import BodyOrientationChanged
        await app.events.publish(BodyOrientationChanged(
            source="reflex:test", previous_yaw_degrees=0, previous_pitch_degrees=0,
            yaw_degrees=1, pitch_degrees=0,
        ))
        await backend.started.wait(); await self.schedule(app); await self.timer.advance()
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(app.temporal_followup_status().state, "due_pending")
        self.assertIn("state:         due_pending", RuntimeConsole(app).execute("followup")[0])
        result, _ = app._execute_self_inspection(
            CognitionToolCall("inspect_self", '{"area":"runtime"}')
        )
        facts = {fact["name"]: fact["value"] for fact in json.loads(result.output)["facts"]}
        self.assertEqual(facts["temporal_followup_pending"], "true")
        self.assertNotIn(SCHEDULE_FOLLOWUP_TOOL, app.effect_tools())
        rejected = await self.schedule(
            app, '{"delay_seconds":40,"purpose":"must not replace handoff"}'
        )
        self.assertEqual(json.loads(rejected.output)["status"], "rejected")
        self.assertEqual(app.temporal_followup_status().state, "due_pending")
        backend.release.set()
        for _ in range(100):
            if len(backend.requests) == 2 and app.attention.status().state != "in_flight":
                break
            await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 2)
        self.assertIsNone(app.temporal.pending)
        await app.stop()

    async def test_new_observation_winning_release_race_preserves_due_slot(self):
        events = HoldingEventBus()
        backend = Backend(blocked=True)
        app = self.make_app(backend=backend, events=events)
        await app.start(); app.set_goal("goal"); await self.schedule(app)
        await self.timer.advance()
        self.assertEqual(app.temporal_followup_status().state, "due_pending")

        async def finished():
            return None

        old_task = asyncio.create_task(finished())
        await old_task
        app.attention._task = old_task
        app.attention._attention_done(old_task)

        from embodied_runtime.events import BodyOrientationChanged
        await app.attention._on_body_event(BodyOrientationChanged(
            source="reflex:test", previous_yaw_degrees=0, previous_pitch_degrees=0,
            yaw_degrees=1, pitch_degrees=0,
        ))
        await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(app.temporal_followup_status().state, "due_pending")
        self.assertNotIn(SCHEDULE_FOLLOWUP_TOOL, app.effect_tools())
        rejected = await self.schedule(app)
        self.assertEqual(json.loads(rejected.output)["status"], "rejected")

        backend.release.set()
        for _ in range(100):
            if len(backend.requests) == 2 and app.attention.status().state != "in_flight":
                break
            await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(app.temporal_followup_status().state, "none")
        await app.stop()

    async def test_goal_clear_discards_due_handoff(self):
        backend = Backend(blocked=True)
        app = self.make_app(backend=backend)
        await app.start(); app.set_goal("goal")
        from embodied_runtime.events import BodyOrientationChanged
        await app.events.publish(BodyOrientationChanged(
            source="reflex:test", previous_yaw_degrees=0, previous_pitch_degrees=0,
            yaw_degrees=1, pitch_degrees=0,
        ))
        await backend.started.wait(); await self.schedule(app); await self.timer.advance()
        self.assertEqual(app.temporal_followup_status().state, "due_pending")
        app.clear_goal()
        self.assertEqual(app.temporal_followup_status().state, "none")
        backend.release.set()
        for _ in range(10): await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 1)
        await app.stop()

    async def test_due_handoff_is_removed_by_replace_resolve_or_shutdown(self):
        for operation in ("replace", "resolve", "shutdown"):
            with self.subTest(operation=operation):
                backend = Backend(blocked=True)
                app = self.make_app(backend=backend)
                await app.start(); app.set_goal("Goal A")
                from embodied_runtime.events import BodyOrientationChanged
                await app.events.publish(BodyOrientationChanged(
                    source="reflex:test", previous_yaw_degrees=0,
                    previous_pitch_degrees=0, yaw_degrees=1, pitch_degrees=0,
                ))
                await backend.started.wait(); await self.schedule(app)
                await self.timer.advance()
                self.assertEqual(app.temporal_followup_status().state, "due_pending")
                if operation == "replace":
                    app.clear_goal(); app.set_goal("Goal B")
                elif operation == "resolve":
                    app.resolve_goal("completed")
                else:
                    await app.stop()
                self.assertEqual(app.temporal_followup_status().state, "none")
                backend.release.set()
                for _ in range(20): await asyncio.sleep(0)
                self.assertEqual(len(backend.requests), 1)
                if operation != "shutdown": await app.stop()

    async def test_provider_failure_releases_one_deferred_temporal_episode(self):
        backend = FailingAfterReleaseBackend(blocked=True)
        app = self.make_app(backend=backend)
        await app.start(); app.set_goal("goal")
        from embodied_runtime.events import BodyOrientationChanged
        await app.events.publish(BodyOrientationChanged(
            source="reflex:test", previous_yaw_degrees=0, previous_pitch_degrees=0,
            yaw_degrees=1, pitch_degrees=0,
        ))
        await backend.started.wait(); await self.schedule(app); await self.timer.advance()
        self.assertEqual(app.temporal_followup_status().state, "due_pending")
        backend.release.set()
        for _ in range(100):
            if len(backend.requests) == 2 and app.attention.status().state != "in_flight":
                break
            await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(app.temporal_followup_status().state, "none")
        await app.stop()

    async def test_non_temporal_event_remains_lossy_while_busy(self):
        backend = Backend(blocked=True)
        app = self.make_app(backend=backend)
        await app.start(); app.set_goal("goal")
        from embodied_runtime.events import BodyOrientationChanged
        event = BodyOrientationChanged(
            source="reflex:test", previous_yaw_degrees=0, previous_pitch_degrees=0,
            yaw_degrees=1, pitch_degrees=0,
        )
        await app.events.publish(event); await backend.started.wait()
        await app.events.publish(event)
        from embodied_runtime.events import ThermalWarningRaised
        await app.attention._on_platform_event(ThermalWarningRaised(
            source="platform_monitor", cpu_temperature_celsius=85.0,
            warning_threshold_celsius=80.0,
        ))
        backend.release.set()
        for _ in range(20): await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 1)
        await app.stop()
