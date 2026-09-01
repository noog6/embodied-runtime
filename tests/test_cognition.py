import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.cognition import CognitionError, TextCognitionBackend
from embodied_runtime.cognition.openai_responses import (
    DEFAULT_MODEL,
    OpenAIResponsesBackend,
)
from embodied_runtime.events import EventBus
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile
from tests.test_platform import snapshot


class StaticPlatform:
    def snapshot(self):
        return snapshot()


class FakeCognition(TextCognitionBackend):
    identifier = "fake-cognition"

    def __init__(self, response="unchanged response"):
        self.response = response
        self.requests = []

    async def respond(self, message, *, instructions=None):
        self.requests.append((message, instructions))
        return self.response


class CognitionApplicationTests(unittest.IsolatedAsyncioTestCase):
    def make_application(self, backend=None, prompt=None, events=None):
        return RobotApplication(
            RobotProfile("test", "Test Robot"),
            VirtualHardwareBackend(),
            ApplicationOptions(startup_prompt=prompt),
            events=events,
            platform_provider=StaticPlatform(),
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
        self.assertEqual(backend.requests, [("hello", "be concise")])
        self.assertIs(app.runtime_state, state)
        self.assertEqual(published, [])
        await app.stop()

    async def test_absent_prompt_is_passed_as_none(self):
        backend = FakeCognition()
        app = self.make_application(backend)
        await app.start()
        await app.request_cognition("hello")
        self.assertEqual(backend.requests, [("hello", None)])
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
