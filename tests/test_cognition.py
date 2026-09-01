import os
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

    async def respond(self, message, *, instructions=None):
        self.requests.append((message, instructions))
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
        message, instructions = backend.requests[0]
        self.assertEqual(message, "hello")
        self.assertTrue(
            instructions.startswith(
                "Operator instructions\n---------------------\n"
                "be concise\n\nRuntime context"
            )
        )
        self.assertIs(app.runtime_state, state)
        self.assertEqual(published, [])
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


class FakeResponses:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def create(self, **arguments):
        self.calls.append(arguments)
        if self.error:
            raise self.error
        return SimpleNamespace(output_text="provider text")


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
