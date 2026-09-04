import asyncio
import json
import unittest

from embodied_runtime.app import (
    OUTCOME_EVALUATION_REQUEST, VISUAL_FOLLOWUP_REQUEST, ApplicationOptions,
    OBSERVE_SCENE_TOOL, RobotApplication,
)
from embodied_runtime.attention import (
    ACTION_INITIATIVE_REQUEST, CONTINUATION_INITIATIVE_REQUEST, AttentionStimulus,
)
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cognition import CognitionError, CognitionToolCall
from embodied_runtime.inspection import SelfInspectionFact, SelfInspectionResult
from embodied_runtime.interaction import OperatorMessageSink
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.events import BodyOrientationChanged
from embodied_runtime.perception import (
    MAX_CAMERA_FRAME_BYTES, OpenAIResponsesVisualPerceptionBackend,
    VisualPerceptionBackend, VisualPerceptionResult,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.sensing.camera import CameraBackend, CameraFrame
from embodied_runtime.state import LifecycleState
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


class Camera(CameraBackend):
    identifier = "fake-camera"
    is_physical = False

    def __init__(self, data=b"jpeg", fail=False):
        self.running = False
        self.data = data
        self.captures = 0
        self.fail = fail

    @property
    def is_running(self):
        return self.running

    def start(self): self.running = True
    def stop(self): self.running = False
    def capture_frame(self):
        self.captures += 1
        if self.fail:
            raise RuntimeError("capture failed")
        return CameraFrame(self.data, "image/jpeg", 1, 1, 1)


class Vision(VisualPerceptionBackend):
    identifier = "fake-vision"

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def interpret(self, frame, focus):
        self.calls.append((frame, focus))
        if self.fail:
            raise RuntimeError("failed")
        return VisualPerceptionResult(focus, "A bounded scene.")


class Cognition:
    identifier = "fake-cognition"

    async def respond(self, message, *, tools=(), tool_executor=None, **kwargs):
        self.tools = tools
        result = await tool_executor(CognitionToolCall(
            "observe_scene", '{"focus":"  Briefly look.  "}'
        ))
        self.result = json.loads(result.output)
        return "done"


class SequenceCognition:
    identifier = "sequence"

    def __init__(self, handlers):
        self.handlers = list(handlers)
        self.requests = []
        self.results = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, **kwargs):
        self.requests.append((message, instructions, tuple(t.name for t in tools)))
        return await self.handlers.pop(0)(self, tool_executor)


def invoke(name, arguments):
    async def handler(backend, executor):
        result = await executor(CognitionToolCall(name, json.dumps(arguments)))
        backend.results.append(json.loads(result.output))
        return "done"
    return handler


async def no_tool(backend, executor):
    return "no effect"


class Sink(OperatorMessageSink):
    def __init__(self):
        self.messages = []

    async def deliver(self, message):
        self.messages.append(message)


class Inspector:
    def __init__(self):
        self.calls = []

    def inspect(self, area):
        self.calls.append(area)
        return SelfInspectionResult(area, (SelfInspectionFact("ok", "true"),))


class VisualPerceptionTests(unittest.IsolatedAsyncioTestCase):
    def app(self, camera=None, vision=None, cognition=None):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=Platform(), camera_backend=camera,
            cognition_backend=cognition, visual_perception_backend=vision,
        )

    def initiative_app(self, backend, camera, vision, *, sink=None, inspector=None):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(
                initiative_enabled=True, initiative_actions_enabled=True,
                initiative_messages_enabled=True,
                initiative_continuation_enabled=True,
                initiative_goal_closure_enabled=True,
            ), platform_provider=Platform(), camera_backend=camera,
            visual_perception_backend=vision, cognition_backend=backend,
            body_backend=VirtualBodyBackend(), operator_message_sink=sink or Sink(),
            self_inspector=inspector,
        )

    @staticmethod
    def stimulus():
        return AttentionStimulus("body_orientation_changed", "reflex:test", 1, 1, 0, 0)

    def test_schema_is_closed_and_bounded(self):
        self.assertEqual(OBSERVE_SCENE_TOOL.name, "observe_scene")
        self.assertFalse(OBSERVE_SCENE_TOOL.parameters["additionalProperties"])
        focus = OBSERVE_SCENE_TOOL.parameters["properties"]["focus"]
        self.assertEqual((focus["minLength"], focus["maxLength"]), (1, 300))

    async def test_projection_requires_running_camera_and_backend(self):
        camera, vision = Camera(), Vision()
        app = self.app(camera, vision)
        self.assertNotIn(OBSERVE_SCENE_TOOL, app.cognition_tools())
        await app.start()
        self.assertIn(OBSERVE_SCENE_TOOL, app.cognition_tools())
        camera.running = False
        self.assertNotIn(OBSERVE_SCENE_TOOL, app.cognition_tools())
        await app.stop()

    async def test_operator_request_captures_and_interprets_once_without_state_change(self):
        camera, vision, cognition = Camera(), Vision(), Cognition()
        app = self.app(camera, vision, cognition)
        await app.start()
        before = app.runtime_state
        await app.request_cognition("What do you see?")
        self.assertEqual((camera.captures, len(vision.calls)), (1, 1))
        self.assertEqual(cognition.result["status"], "applied")
        self.assertIs(app.runtime_state, before)
        await app.stop()

    async def test_invalid_focus_and_oversize_never_call_backend(self):
        for arguments in ({"focus": " "}, {"focus": 2},
                          {"focus": "x\n"}, {"focus": "x\u0085"},
                          {"focus": "x", "camera": "other"},
                          {"focus": "x" * 301}):
            camera, vision = Camera(), Vision()
            app = self.app(camera, vision)
            await app.start()
            result, semantic = await app._execute_visual_perception(
                CognitionToolCall("observe_scene", json.dumps(arguments))
            )
            self.assertEqual(json.loads(result.output)["status"], "rejected")
            self.assertIsNone(semantic)
            self.assertEqual(len(vision.calls), 0)
            await app.stop()
        camera, vision = Camera(b"x" * (MAX_CAMERA_FRAME_BYTES + 1)), Vision()
        app = self.app(camera, vision)
        await app.start()
        await app._execute_visual_perception(
            CognitionToolCall("observe_scene", '{"focus":"look"}')
        )
        self.assertEqual((camera.captures, len(vision.calls)), (1, 0))
        await app.stop()

    async def test_openai_transport_is_in_memory_and_reports_truncation(self):
        class Responses:
            async def create(inner, **kwargs):
                inner.arguments = kwargs
                return type("Response", (), {"output_text": "x" * 2001})()
        responses = Responses()
        client = type("Client", (), {"responses": responses})()
        backend = OpenAIResponsesVisualPerceptionBackend(client=client, model="vision")
        result = await backend.interpret(
            CameraFrame(b"abc", "image/jpeg", 1, 1, 1), "look"
        )
        image = responses.arguments["input"][0]["content"][1]["image_url"]
        self.assertEqual(image, "data:image/jpeg;base64,YWJj")
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.description), 2000)
        self.assertIn("visible evidence only", responses.arguments["instructions"])

    async def test_capture_and_visual_provider_failures_stop_without_followup(self):
        for camera, vision, expected in (
            (Camera(fail=True), Vision(), (1, 0)),
            (Camera(), Vision(fail=True), (1, 1)),
        ):
            backend = SequenceCognition([invoke("observe_scene", {"focus": "look"})])
            app = self.initiative_app(backend, camera, vision)
            await app.start()
            app.set_goal("Stay aware")
            await app._request_initiative(self.stimulus())
            self.assertEqual((camera.captures, len(vision.calls)), expected)
            self.assertEqual(len(backend.requests), 1)
            self.assertEqual(app.attention.status().last_continuation_state, "not_run")
            self.assertEqual(app.attention.status().last_outcome_state, "not_run")
            await app.stop()

    async def test_visual_only_episode_preserves_goal_and_memory(self):
        backend = SequenceCognition([
            invoke("observe_scene", {"focus": "look"}), no_tool,
        ])
        camera, vision = Camera(), Vision()
        app = self.initiative_app(backend, camera, vision)
        await app.start()
        goal = app.set_goal("Stay aware")
        memory = app.working_memory.snapshot()
        outcome = await app._request_initiative(self.stimulus())
        self.assertEqual((outcome.action, outcome.action_status), (None, None))
        self.assertEqual([r[0] for r in backend.requests], [
            ACTION_INITIATIVE_REQUEST, VISUAL_FOLLOWUP_REQUEST,
        ])
        self.assertIs(app.active_goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        self.assertEqual(app.attention.status().last_outcome_state, "not_run")
        await app.stop()

    async def test_direct_effect_and_inspection_paths_do_not_capture(self):
        for first, inspector_calls in (
            (invoke("orient_body", {"yaw_degrees": 5, "pitch_degrees": 0}), []),
            (invoke("inspect_self", {"area": "storage"}), ["storage"]),
            (invoke("inspect_self", {"area": "camera"}), []),
        ):
            camera, vision, inspector = Camera(), Vision(), Inspector()
            handlers = [first, no_tool, no_tool, no_tool]
            backend = SequenceCognition(handlers)
            app = self.initiative_app(backend, camera, vision, inspector=inspector)
            await app.start()
            app.set_goal("Maintain")
            await app._request_initiative(self.stimulus())
            self.assertEqual((camera.captures, len(vision.calls)), (0, 0))
            self.assertEqual(inspector.calls, inspector_calls)
            await app.stop()

    async def test_post_visual_exposes_effects_only_and_rejects_acquisition(self):
        async def try_acquisitions(backend, executor):
            backend.requests[-1] += ()
            for name, arguments in (("inspect_self", {"area": "runtime"}),
                                    ("observe_scene", {"focus": "again"})):
                result = await executor(CognitionToolCall(name, json.dumps(arguments)))
                backend.results.append(json.loads(result.output))
            return "done"

        backend = SequenceCognition([
            invoke("observe_scene", {"focus": "look"}), try_acquisitions,
        ])
        camera, vision = Camera(), Vision()
        app = self.initiative_app(backend, camera, vision)
        await app.start()
        app.set_goal("Maintain")
        await app._request_initiative(self.stimulus())
        self.assertNotIn("inspect_self", backend.requests[1][2])
        self.assertNotIn("observe_scene", backend.requests[1][2])
        self.assertEqual([item["status"] for item in backend.results[-2:]],
                         ["rejected", "rejected"])
        self.assertEqual(camera.captures, 1)
        await app.stop()

    async def test_goal_and_stopping_races_reject_stale_effect(self):
        for stop in (False, True):
            sink = Sink()

            async def race(backend, executor, stopping=stop):
                if stopping:
                    app._set_lifecycle(LifecycleState.STOPPING)
                else:
                    app.clear_goal()
                    app.set_goal("Goal B")
                result = await executor(CognitionToolCall(
                    "address_operator", '{"message":"stale"}'
                ))
                backend.results.append(json.loads(result.output))
                return "done"

            backend = SequenceCognition([
                invoke("observe_scene", {"focus": "look"}), race,
            ])
            app = self.initiative_app(backend, Camera(), Vision(), sink=sink)
            await app.start()
            app.set_goal("Goal A")
            await app._request_initiative(self.stimulus())
            self.assertEqual(sink.messages, [])
            self.assertEqual(backend.results[-1]["status"], "rejected")
            self.assertEqual(len(backend.requests), 2)
            await app.stop()

    async def test_failed_followup_finalization_preserves_applied_effect(self):
        sink = Sink()

        async def apply_then_fail(backend, executor):
            result = await executor(CognitionToolCall(
                "address_operator", '{"message":"I see it."}'
            ))
            backend.results.append(json.loads(result.output))
            raise CognitionError("finalization failed")

        backend = SequenceCognition([
            invoke("observe_scene", {"focus": "look"}), apply_then_fail,
        ])
        app = self.initiative_app(backend, Camera(), Vision(), sink=sink)
        await app.start()
        app.set_goal("Maintain")
        outcome = await app._request_initiative(self.stimulus())
        self.assertEqual((outcome.action, outcome.action_status),
                         ("address_operator", "applied"))
        self.assertEqual(len(sink.messages), 1)
        self.assertEqual(len(backend.requests), 2)
        await app.stop()

    async def test_post_visual_effect_uses_perception_log_category(self):
        backend = SequenceCognition([
            invoke("observe_scene", {"focus": "look"}),
            invoke("address_operator", {"message": "Visible."}),
            no_tool, no_tool,
        ])
        app = self.initiative_app(backend, Camera(), Vision())
        await app.start()
        app.set_goal("Maintain")
        with self.assertLogs("embodied_runtime.app", level="INFO") as logs:
            await app._request_initiative(self.stimulus())
        rendered = "\n".join(logs.output)
        self.assertIn("[PERCEPTION] tool=address_operator status=applied", rendered)
        self.assertNotIn("[INSPECTION] tool=address_operator", rendered)
        await app.stop()

    async def test_compound_visual_sequence_has_two_effects_and_stops(self):
        sink, captured = Sink(), []

        async def outcome(backend, executor):
            captured.append(backend.requests[-1][1])
            return "keep active"

        backend = SequenceCognition([
            invoke("observe_scene", {"focus": "look"}),
            invoke("address_operator", {"message": "I see the operator."}),
            invoke("orient_body", {"yaw_degrees": 45, "pitch_degrees": -20}),
            outcome,
        ])
        camera, vision = Camera(), Vision()
        app = self.initiative_app(backend, camera, vision, sink=sink)
        await app.start()
        goal = app.set_goal("Maintain 45/-20")
        memory = app.working_memory.snapshot()
        await app.set_body_orientation(yaw_degrees=45, pitch_degrees=-20)
        await app.events.publish(BodyOrientationChanged(
            source="reflex:test", previous_yaw_degrees=45,
            previous_pitch_degrees=-20, yaw_degrees=0, pitch_degrees=0,
        ))
        for _ in range(100):
            if len(backend.requests) == 4 and app.attention.status().state != "in_flight":
                break
            await asyncio.sleep(0)
        self.assertEqual([r[0] for r in backend.requests], [
            ACTION_INITIATIVE_REQUEST, VISUAL_FOLLOWUP_REQUEST,
            CONTINUATION_INITIATIVE_REQUEST, OUTCOME_EVALUATION_REQUEST,
        ])
        self.assertIn("effect_1_name: address_operator", captured[0])
        self.assertIn("effect_2_name: orient_body", captured[0])
        self.assertNotIn("effect_1_name: observe_scene", captured[0])
        self.assertEqual((camera.captures, len(vision.calls), len(sink.messages)),
                         (1, 1, 1))
        self.assertEqual((app.runtime_state.body.yaw_degrees,
                          app.runtime_state.body.pitch_degrees), (45, -20))
        self.assertIs(app.active_goal, goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        self.assertTrue(app.attention._task.done())
        await app.stop()
