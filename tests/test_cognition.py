import os
import json
import sys
from dataclasses import FrozenInstanceError, fields
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cognition import (
    CognitionContext,
    CognitionError,
    CognitionToolCall,
    CognitionToolResult,
    TextCognitionBackend,
    compose_cognition_instructions,
)
from embodied_runtime.cognition.openai_responses import (
    DEFAULT_MODEL,
    OpenAIResponsesBackend,
)
from embodied_runtime.events import EventBus
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile
from embodied_runtime.sensing.camera import CameraBackend, CameraFrame
from tests.test_platform import snapshot


class StaticPlatform:
    def __init__(self):
        self.current = snapshot(
            model="Test Model",
            uptime_seconds=12.5,
            load_averages=(0.1, 0.2, 0.3),
            memory_total_bytes=512 * 1024 * 1024,
            memory_available_bytes=256 * 1024 * 1024,
            cpu_temperature_celsius=42.5,
        )

    def snapshot(self):
        return self.current


class FakeCamera(CameraBackend):
    identifier = "fake-camera"
    is_physical = False

    def __init__(self):
        self.running = False
        self.captures = 0

    @property
    def is_running(self):
        return self.running

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def capture_frame(self):
        self.captures += 1
        return CameraFrame(b"image", "image/jpeg", 1, 1, 1)


class FakeCognition(TextCognitionBackend):
    identifier = "fake-cognition"

    def __init__(self, response="unchanged response"):
        self.response = response
        self.requests = []

    async def respond(
        self, message, *, instructions=None, tools=(), tool_executor=None,
        refreshed_instructions=None,
    ):
        self.requests.append((message, instructions, tools, tool_executor,
                              refreshed_instructions))
        return self.response


class CognitionApplicationTests(unittest.IsolatedAsyncioTestCase):
    def make_application(
        self,
        backend=None,
        prompt=None,
        events=None,
        platform=None,
        body=None,
        camera=None,
    ):
        return RobotApplication(
            RobotProfile("test", "Test Robot", "A test robot."),
            VirtualHardwareBackend(),
            ApplicationOptions(startup_prompt=prompt),
            events=events,
            platform_provider=platform or StaticPlatform(),
            body_backend=body,
            camera_backend=camera,
            cognition_backend=backend,
        )

    async def test_request_is_single_turn_and_propagates_text_and_instructions(self):
        backend = FakeCognition()
        app = self.make_application(backend, "be concise")
        await app.start()
        state = app.runtime_state
        published = []
        original_publish = app.events.publish

        async def record(event):
            published.append(event)
            await original_publish(event)

        app.events.publish = record
        self.assertEqual(await app.request_cognition("hello"), "unchanged response")
        message, instructions, tools, executor, refresh = backend.requests[0]
        self.assertEqual(message, "hello")
        self.assertTrue(
            instructions.startswith(
                "Operator instructions\n---------------------\n"
                "be concise\n\nRuntime context"
            )
        )
        self.assertIs(app.runtime_state, state)
        self.assertEqual(published, [])
        self.assertEqual(tools, ())
        self.assertIsNone(executor)
        self.assertIsNone(refresh)
        await app.stop()

    async def test_absent_prompt_still_passes_runtime_context(self):
        backend = FakeCognition()
        app = self.make_application(backend)
        await app.start()
        await app.request_cognition("hello")
        self.assertEqual(backend.requests[0][0], "hello")
        self.assertTrue(backend.requests[0][1].startswith("Runtime context\n"))
        await app.stop()

    async def test_context_is_fresh_for_body_presence_and_platform(self):
        backend = FakeCognition()
        platform = StaticPlatform()
        app = self.make_application(
            backend, platform=platform, body=VirtualBodyBackend()
        )
        await app.start()
        await app.request_cognition("first")
        await app.set_body_orientation(yaw_degrees=-20, pitch_degrees=10)
        await app.observe_presence(present=True, source="virtual_scenario")
        platform.current = snapshot(hostname="new-host", model="New Model")
        app.refresh_platform_state()
        await app.request_cognition("second")
        first = backend.requests[0][1]
        second = backend.requests[1][1]
        self.assertIn("yaw_deg: 0.0", first)
        self.assertIn("status: unknown", first)
        self.assertIn("hostname: test-host", first)
        self.assertIn("yaw_deg: -20.0", second)
        self.assertIn("pitch_deg: 10.0", second)
        self.assertIn("status: present", second)
        self.assertIn("source: virtual_scenario", second)
        self.assertIn("hostname: new-host", second)
        await app.observe_presence(present=False, source="virtual_scenario")
        await app.request_cognition("third")
        self.assertIn("status: absent", backend.requests[2][1])
        await app.stop()

    async def test_camera_metadata_does_not_capture(self):
        backend = FakeCognition()
        camera = FakeCamera()
        app = self.make_application(backend, camera=camera)
        await app.start()
        await app.request_cognition("camera?")
        instructions = backend.requests[0][1]
        self.assertIn("state: configured", instructions)
        self.assertIn("backend: fake-camera", instructions)
        self.assertIn("physical: false", instructions)
        self.assertIn("running: true", instructions)
        self.assertEqual(camera.captures, 0)
        await app.stop()

    async def test_request_validation_and_lifecycle(self):
        backend = FakeCognition()
        app = self.make_application(backend)
        with self.assertRaisesRegex(RuntimeError, "running"):
            await app.request_cognition("hello")
        await app.start()
        for message in ("", "   "):
            with self.assertRaisesRegex(ValueError, "non-empty"):
                await app.request_cognition(message)
        await app.stop()

    async def test_missing_backend_is_clear(self):
        app = self.make_application()
        await app.start()
        with self.assertRaisesRegex(RuntimeError, "No cognition backend"):
            await app.request_cognition("hello")
        await app.stop()

    async def test_tool_definition_and_request_time_safety_gate(self):
        virtual = VirtualBodyBackend()
        app = self.make_application(FakeCognition(), body=virtual)
        (tool,) = app.cognition_tools()
        self.assertEqual(tool.name, "orient_body")
        self.assertEqual(set(tool.parameters["properties"]), {
            "yaw_degrees", "pitch_degrees",
        })
        self.assertEqual(set(tool.parameters["required"]), {
            "yaw_degrees", "pitch_degrees",
        })
        self.assertFalse(tool.parameters["additionalProperties"])
        self.assertEqual(self.make_application(FakeCognition()).cognition_tools(), ())

        class NoOrientation(VirtualBodyBackend):
            capabilities = ()

        class PhysicalOrientation(VirtualBodyBackend):
            is_physical = True

        self.assertEqual(
            self.make_application(FakeCognition(), body=NoOrientation()).cognition_tools(),
            (),
        )
        physical = self.make_application(
            FakeCognition(), body=PhysicalOrientation()
        )
        self.assertEqual(physical.cognition_tools(), ())
        await physical.start()
        result = await physical._execute_cognition_tool(CognitionToolCall(
            "orient_body", '{"yaw_degrees":35,"pitch_degrees":-10}'
        ))
        self.assertEqual(json.loads(result.output)["status"], "rejected")
        self.assertEqual(physical.runtime_state.body.yaw_degrees, 0.0)
        await physical.stop()

    async def test_dispatch_validation_success_and_rejection_preserve_state(self):
        app = self.make_application(FakeCognition(), body=VirtualBodyBackend())
        await app.start()
        applied = await app._execute_cognition_tool(CognitionToolCall(
            "orient_body", '{"yaw_degrees":35,"pitch_degrees":-10}'
        ))
        self.assertEqual(json.loads(applied.output), {
            "status": "applied", "yaw_degrees": 35.0, "pitch_degrees": -10.0,
        })
        previous = app.runtime_state.body
        invalid = (
            ("orient_body", "{"),
            ("orient_body", "[]"),
            ("orient_body", '{"yaw_degrees":1}'),
            ("orient_body", '{"yaw_degrees":1,"pitch_degrees":2,"extra":3}'),
            ("orient_body", '{"yaw_degrees":"1","pitch_degrees":2}'),
            ("orient_body", '{"yaw_degrees":true,"pitch_degrees":2}'),
            ("orient_body", '{"yaw_degrees":500,"pitch_degrees":0}'),
            ("unknown", '{"yaw_degrees":1,"pitch_degrees":2}'),
        )
        for name, arguments in invalid:
            with self.subTest(name=name, arguments=arguments):
                rejected = await app._execute_cognition_tool(
                    CognitionToolCall(name, arguments)
                )
                self.assertEqual(json.loads(rejected.output)["status"], "rejected")
                self.assertIs(app.runtime_state.body, previous)
        await app.stop()

    async def test_application_supplies_refreshed_authoritative_grounding(self):
        class ToolCognition(FakeCognition):
            async def respond(
                self, message, *, instructions=None, tools=(), tool_executor=None,
                refreshed_instructions=None,
            ):
                self.requests.append((instructions, tools))
                result = await tool_executor(CognitionToolCall(
                    "orient_body", '{"yaw_degrees":35,"pitch_degrees":-10}'
                ))
                self.result = result
                self.after = refreshed_instructions()
                return "done"

        backend = ToolCognition()
        app = self.make_application(backend, body=VirtualBodyBackend())
        await app.start()
        await app.request_cognition("move")
        self.assertIn("yaw_deg: 0.0", backend.requests[0][0])
        self.assertIn("pitch_deg: 0.0", backend.requests[0][0])
        self.assertIn("yaw_deg: 35.0", backend.after)
        self.assertIn("pitch_deg: -10.0", backend.after)
        await app.stop()

    async def test_rejected_action_refreshes_unchanged_authoritative_grounding(self):
        class RejectingCognition(FakeCognition):
            async def respond(
                self, message, *, instructions=None, tools=(), tool_executor=None,
                refreshed_instructions=None,
            ):
                result = await tool_executor(CognitionToolCall(
                    "orient_body", '{"yaw_degrees":500,"pitch_degrees":0}'
                ))
                self.result = json.loads(result.output)
                self.after = refreshed_instructions()
                return "rejected"

        backend = RejectingCognition()
        app = self.make_application(backend, body=VirtualBodyBackend())
        await app.start()
        await app.request_cognition("invalid move")
        self.assertEqual(backend.result["status"], "rejected")
        self.assertIn("yaw_deg: 0.0", backend.after)
        self.assertIn("pitch_deg: 0.0", backend.after)
        self.assertEqual(app.runtime_state.body.yaw_degrees, 0.0)
        await app.stop()


class FakeResponses:
    def __init__(self, error=None, results=None):
        self.calls = []
        self.error = error
        self.results = list(results or [])

    async def create(self, **arguments):
        self.calls.append(arguments)
        if self.error:
            raise self.error
        if self.results:
            return self.results.pop(0)
        return SimpleNamespace(output_text="provider text", output=[], id="response")


class OpenAIResponsesTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_shape_and_output_text(self):
        responses = FakeResponses()
        backend = OpenAIResponsesBackend(
            model="test-model", client=SimpleNamespace(responses=responses)
        )
        self.assertEqual(
            await backend.respond("operator text", instructions="startup"),
            "provider text",
        )
        self.assertEqual(
            responses.calls,
            [{"model": "test-model", "input": "operator text", "instructions": "startup"}],
        )

    async def test_one_function_call_executes_once_and_continues_with_result(self):
        first = SimpleNamespace(
            id="response-1", output_text="", output=[SimpleNamespace(
                type="function_call", name="orient_body", call_id="call-7",
                arguments='{"yaw_degrees":35,"pitch_degrees":-10}',
            )],
        )
        final = SimpleNamespace(id="response-2", output_text="applied", output=[])
        responses = FakeResponses(results=[first, final])
        backend = OpenAIResponsesBackend(
            model="test-model", client=SimpleNamespace(responses=responses)
        )
        calls = []

        async def execute(call):
            calls.append(call)
            return CognitionToolResult('{"status":"applied"}')

        tool = self._tool()
        result = await backend.respond(
            "move", instructions="before", tools=(tool,), tool_executor=execute,
            refreshed_instructions=lambda: "after",
        )
        self.assertEqual(result, "applied")
        self.assertEqual(calls, [CognitionToolCall(
            "orient_body", '{"yaw_degrees":35,"pitch_degrees":-10}'
        )])
        initial, continuation = responses.calls
        self.assertEqual(initial["tool_choice"], "auto")
        self.assertFalse(initial["parallel_tool_calls"])
        self.assertEqual([item["name"] for item in initial["tools"]], ["orient_body"])
        self.assertEqual(continuation["previous_response_id"], "response-1")
        self.assertEqual(continuation["instructions"], "after")
        self.assertEqual(continuation["tool_choice"], "none")
        self.assertEqual(continuation["input"], [{
            "type": "function_call_output", "call_id": "call-7",
            "output": '{"status":"applied"}',
        }])

    async def test_multiple_calls_execute_none(self):
        call = lambda identifier: SimpleNamespace(
            type="function_call", name="orient_body", call_id=identifier,
            arguments="{}",
        )
        responses = FakeResponses(results=[SimpleNamespace(
            id="response", output_text="", output=[call("a"), call("b")]
        )])
        backend = OpenAIResponsesBackend(client=SimpleNamespace(responses=responses))
        executions = []
        with self.assertRaisesRegex(CognitionError, "multiple"):
            await backend.respond(
                "move", tools=(self._tool(),),
                tool_executor=lambda invocation: executions.append(invocation),
                refreshed_instructions=lambda: "fresh",
            )
        self.assertEqual(executions, [])

    async def test_text_response_with_tools_does_not_execute(self):
        responses = FakeResponses()
        backend = OpenAIResponsesBackend(client=SimpleNamespace(responses=responses))
        executions = []
        self.assertEqual(await backend.respond(
            "question", tools=(self._tool(),),
            tool_executor=lambda invocation: executions.append(invocation),
            refreshed_instructions=lambda: "fresh",
        ), "provider text")
        self.assertEqual(executions, [])

    async def test_continuation_is_scoped_to_each_request(self):
        responses = FakeResponses()
        backend = OpenAIResponsesBackend(client=SimpleNamespace(responses=responses))
        await backend.respond("first")
        await backend.respond("second")
        self.assertNotIn("previous_response_id", responses.calls[0])
        self.assertNotIn("previous_response_id", responses.calls[1])

    @staticmethod
    def _tool():
        from embodied_runtime.app import ORIENT_BODY_TOOL
        return ORIENT_BODY_TOOL

    async def test_absent_instructions_are_omitted(self):
        responses = FakeResponses()
        backend = OpenAIResponsesBackend(client=SimpleNamespace(responses=responses))
        await backend.respond("hello")
        self.assertEqual(responses.calls, [{"model": DEFAULT_MODEL, "input": "hello"}])

    async def test_provider_failure_crosses_project_boundary(self):
        backend = OpenAIResponsesBackend(
            client=SimpleNamespace(responses=FakeResponses(RuntimeError("secret detail")))
        )
        with self.assertRaisesRegex(CognitionError, "OpenAI Responses request failed") as caught:
            await backend.respond("hello")
        self.assertNotIn("secret detail", str(caught.exception))

    def test_default_and_environment_model(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(OpenAIResponsesBackend(client=object()).model, DEFAULT_MODEL)
        with patch.dict(os.environ, {"OPENAI_MODEL": "operator-model"}, clear=True):
            self.assertEqual(OpenAIResponsesBackend(client=object()).model, "operator-model")

    def test_core_import_does_not_load_openai(self):
        previous = sys.modules.pop("openai", None)
        try:
            sys.modules.pop("embodied_runtime.cognition", None)
            __import__("embodied_runtime.cognition")
            self.assertNotIn("openai", sys.modules)
        finally:
            if previous is not None:
                sys.modules["openai"] = previous


class CognitionContextTests(unittest.TestCase):
    def make_context(self):
        app = RobotApplication(
            RobotProfile("test", "Test Robot", "Description"),
            VirtualHardwareBackend(),
            platform_provider=StaticPlatform(),
        )
        return app.cognition_context()

    def test_projection_is_immutable_and_allow_listed(self):
        context = self.make_context()
        with self.assertRaises(FrozenInstanceError):
            context.profile_name = "changed"  # type: ignore[misc]
        names = {field.name for field in fields(CognitionContext)}
        self.assertEqual(len(names), 28)
        self.assertFalse(names & {"environment", "api_key", "captured_monotonic"})
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}):
            self.assertNotIn("secret", self.make_context().render())

    def test_missing_values_are_explicit_and_rendering_is_deterministic(self):
        context = self.make_context()
        first = context.render()
        self.assertEqual(first, context.render())
        self.assertIn("lifecycle: created", first)
        self.assertIn("Body\n  state: unavailable", first)
        self.assertIn("Presence\n  status: unknown\n  source: unknown", first)
        self.assertIn("Camera\n  state: unconfigured", first)
        self.assertNotIn("object at 0x", first)
        self.assertIn("metadata describes availability only", first)
        self.assertIn("cannot\ncapture, access, or see images", first)

    def test_platform_identity_hardware_and_compact_memory_are_rendered(self):
        app = RobotApplication(
            RobotProfile("test", "Test Robot", "Description"),
            VirtualHardwareBackend(),
            platform_provider=StaticPlatform(),
        )
        app.refresh_platform_state()
        context = app.cognition_context()
        self.assertEqual(context.profile_description, "Description")
        self.assertEqual(context.platform_hostname, "test-host")
        self.assertEqual(context.hardware_capabilities, ())
        rendered = context.render()
        self.assertIn("memory_total_mib: 512.0", rendered)
        self.assertIn("memory_available_mib: 256.0", rendered)
        self.assertIn("capabilities: none", rendered)

    def test_operator_prompt_is_preserved_and_separated(self):
        prompt = "  Keep this exactly.\nSecond line  "
        composed = compose_cognition_instructions(self.make_context(), prompt)
        self.assertIn(prompt, composed)
        self.assertLess(composed.index("Operator instructions"), composed.index(prompt))
        self.assertLess(composed.index(prompt), composed.index("Runtime context"))
