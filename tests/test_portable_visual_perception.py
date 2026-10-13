"""Offline visual adapters, bounded fallback, and camera integration checks."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from embodied_runtime.app import ApplicationOptions, CAMERA_RESOURCE, RobotApplication
from embodied_runtime.cognition import CognitionToolCall
from embodied_runtime.config import ConfigurationError, VisionBackendConfig, VisionConfiguration
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.observability import RunObservability
from embodied_runtime.perception import (
    OpenAICompatibleVisualPerceptionBackend, OpenAIResponsesVisualPerceptionBackend,
    OrderedVisualPerceptionBackend, VisualPerceptionError,
)
from embodied_runtime.perception.factory import create_visual_perception_backend
from embodied_runtime.perception.visual import MAX_VISUAL_OUTPUT_TOKENS, VISUAL_PROVIDER_INSTRUCTIONS
from embodied_runtime.pricing import BUILT_IN_PRICING
from embodied_runtime.profile import RobotProfile
from embodied_runtime.sensing.camera import CameraFrame
from tests.test_openai_compatible import FakeCompletions, client, response
from tests.test_visual_perception import (
    Camera, Platform, RecordingArbiter, SequenceCognition, invoke, no_tool,
    Vision,
)
from tests import test_visual_perception as visual_helpers


FRAME = CameraFrame(b"same-exact-frame", "image/jpeg", 2, 3, 123)
FOCUS = "Describe visible objects and uncertainty."
PRIMARY = VisionBackendConfig("openai-compatible", "google/gemini-3.8-flash", "TEST_ROUTER_KEY",
                              "https://openrouter.ai/api/v1", 15)
SECONDARY = VisionBackendConfig("openai-responses", "gpt-6.1-sol", "TEST_OPENAI_KEY",
                                request_timeout_seconds=15)
CONFIG = VisionConfiguration(30, (PRIMARY, SECONDARY))
DIRECT_USAGE = {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}


def direct_response(text="A visible object, identity uncertain.", *, status="completed", usage=DIRECT_USAGE,
                    content=None, **fields):
    return {"status": status, "model": "returned/vision-model", "usage": usage,
            "output": [{"type": "message", "role": "assistant", "content": content if content is not None
                         else [{"type": "output_text", "text": text}]}], **fields}


def direct_client(fake):
    return SimpleNamespace(responses=fake)


class ProviderError(Exception):
    def __init__(self, status):
        super().__init__("PRIVATE body Authorization SECRET https://user:SECRET@host?key=SECRET")
        self.status_code = status


class APIResponseValidationError(ProviderError):
    pass


def sdk(constructor=None):
    module = ModuleType("openai")
    module.APIError = ProviderError
    module.AsyncOpenAI = constructor or Mock()
    return module


class PortableVisionTests(unittest.IsolatedAsyncioTestCase):
    def chain(self, primary, secondary=None, *, config=CONFIG):
        first = FakeCompletions(primary)
        second = FakeCompletions(secondary if secondary is not None else direct_response())
        backend = create_visual_perception_backend("configured", config,
                                                   clients=(client(first), direct_client(second)))
        observed = RunObservability(pricing=BUILT_IN_PRICING)
        backend.observability = observed
        return backend, first, second, observed

    async def test_primary_success_and_uncertainty_never_call_secondary(self):
        backend, first, second, observed = self.chain(response("Identity uncertain from this view."))
        result = await backend.interpret(FRAME, FOCUS)
        self.assertIn("uncertain", result.description)
        self.assertEqual(result.focus, FOCUS)
        self.assertEqual(len(first.requests), 1)
        self.assertEqual(second.requests, [])
        request = first.requests[0]
        self.assertEqual(request["messages"][0], {"role": "system", "content": VISUAL_PROVIDER_INSTRUCTIONS})
        self.assertEqual(request["messages"][1]["content"][0], {"type": "text", "text": FOCUS})
        self.assertEqual(request["messages"][1]["content"][1]["image_url"]["url"],
                         "data:image/jpeg;base64,c2FtZS1leGFjdC1mcmFtZQ==")
        self.assertEqual(request["model"], PRIMARY.model)
        self.assertEqual(request["max_tokens"], MAX_VISUAL_OUTPUT_TOKENS)
        self.assertFalse(request["stream"])
        self.assertEqual(request["extra_body"], {"provider": {"require_parameters": True, "allow_fallbacks": False}})
        for parameter in ("tools", "tool_choice", "parallel_tool_calls", "max_completion_tokens"):
            self.assertNotIn(parameter, request)
        record = observed.snapshot()["provider_calls"][0]
        self.assertEqual((record["role"], record["backend"], record["protocol"], record["endpoint"]),
                         ("vision", "openai-compatible", "chat-completions", "https://openrouter.ai"))
        self.assertEqual((record["requested_model"], record["returned_model"], record["attempt_order"]),
                         (PRIMARY.model, "returned/model-id", 1))
        self.assertTrue(record["accepted"])
        self.assertFalse(record["fallback"])
        self.assertEqual(observed.snapshot()["metrics"]["vision_fallbacks"], 0)

    async def test_eligible_errors_switch_once_and_reuse_exact_image_and_focus(self):
        malformed = response()
        malformed["choices"] = []
        for primary, category in (
            (TimeoutError("private"), "timeout"), (ConnectionError("private"), "connection"),
            (ProviderError(429), "rate_limit"), (ProviderError(503), "provider_unavailable"),
            (ProviderError(402), "payment_required"), (ProviderError(404), "model_unavailable"),
            (response(" "), "empty_response"), (malformed, "malformed_response"),
            (response("partial", finish="length"), "truncated"),
            (json.JSONDecodeError("private", "SECRET", 0), "malformed_response"),
            (APIResponseValidationError(200), "malformed_response"),
        ):
            with self.subTest(category=category), patch.dict("sys.modules", {"openai": sdk()}):
                backend, first, second, observed = self.chain(primary)
                result = await backend.interpret(FRAME, FOCUS)
                self.assertIn("uncertain", result.description)
                self.assertEqual((len(first.requests), len(second.requests)), (1, 1))
                chat_content = first.requests[0]["messages"][1]["content"]
                direct_content = second.requests[0]["input"][0]["content"]
                self.assertEqual(chat_content[0]["text"], direct_content[0]["text"])
                self.assertEqual(chat_content[1]["image_url"]["url"], direct_content[1]["image_url"])
                self.assertEqual(second.requests[0]["instructions"], VISUAL_PROVIDER_INSTRUCTIONS)
                self.assertEqual(second.requests[0]["max_output_tokens"], MAX_VISUAL_OUTPUT_TOKENS)
                for parameter in ("tools", "tool_choice", "parallel_tool_calls"):
                    self.assertNotIn(parameter, second.requests[0])
                records = observed.snapshot()["provider_calls"]
                self.assertEqual(records[0]["failure_category"], category)
                status = getattr(primary, "status_code", None)
                self.assertEqual(records[0]["http_status"], status if type(status) is int and 400 <= status <= 599 else None)
                self.assertFalse(records[0]["accepted"])
                self.assertTrue(records[1]["accepted"])
                self.assertEqual(records[1]["attempt_order"], 2)
                self.assertTrue(records[1]["fallback"])
                self.assertEqual(observed.snapshot()["metrics"]["vision_fallbacks"], 1)

    async def test_refusal_and_noneligible_http_failures_stop(self):
        for primary, category in (
            (response("refused", refusal="PRIVATE"), "refused"),
            (response("refused", finish="content_filter"), "refused"),
            (ProviderError(400), "invalid_request"), (ProviderError(401), "authentication"),
        ):
            with self.subTest(category=category), patch.dict("sys.modules", {"openai": sdk()}):
                backend, first, second, _ = self.chain(primary)
                with self.assertRaises(VisualPerceptionError) as caught:
                    await backend.interpret(FRAME, FOCUS)
                self.assertEqual(caught.exception.category, category)
                self.assertEqual(len(first.requests), 1)
                self.assertEqual(second.requests, [])

    async def test_invalid_input_and_programming_errors_do_not_switch(self):
        for frame in (None, replace(FRAME, data=b""), replace(FRAME, data="wrong"),
                      replace(FRAME, data=b"x" * (4 * 1024 * 1024 + 1)),
                      replace(FRAME, media_type="text/plain"), replace(FRAME, width=0),
                      replace(FRAME, captured_at_ns=-1)):
            with self.subTest(frame_type=type(frame)):
                backend, first, second, _ = self.chain(response())
                with self.assertRaises(VisualPerceptionError) as caught:
                    await backend.interpret(frame, FOCUS)
                self.assertEqual(caught.exception.category, "invalid_frame")
                self.assertEqual(first.requests + second.requests, [])
        for error in (RuntimeError("bug"), TypeError("bug"), ValueError("bug"), ConfigurationError("invalid config")):
            backend, first, second, observed = self.chain(error)
            with self.assertRaises(type(error)) as caught:
                await backend.interpret(FRAME, FOCUS)
            self.assertIs(caught.exception, error)
            self.assertEqual(len(first.requests), 1)
            self.assertEqual(second.requests, [])
            self.assertEqual(observed.snapshot()["provider_calls"][0]["failure_category"], "implementation_error")

    async def test_wrapper_passes_identical_frame_object_and_sensor_time_to_every_attempt(self):
        class PrimaryVision(Vision):
            async def interpret(self, frame, focus):
                self.calls.append((frame, focus))
                raise VisualPerceptionError("connection")
        first, second = PrimaryVision(), Vision()
        result = await OrderedVisualPerceptionBackend((first, second)).interpret(FRAME, FOCUS)
        self.assertEqual(result.focus, FOCUS)
        for provider in (first, second):
            self.assertEqual(len(provider.calls), 1)
            self.assertIs(provider.calls[0][0], FRAME)
            self.assertIs(provider.calls[0][0].data, FRAME.data)
            self.assertEqual(provider.calls[0][0].captured_at_ns, 123)
            self.assertEqual(provider.calls[0][0].media_type, "image/jpeg")
            self.assertEqual(provider.calls[0][1], FOCUS)

    async def test_uncategorized_local_timeout_does_not_silently_switch(self):
        class BuggyVision(Vision):
            async def interpret(self, frame, focus):
                raise TimeoutError("unexpected local bug")
        second = Vision()
        with self.assertRaisesRegex(TimeoutError, "unexpected local bug"):
            await OrderedVisualPerceptionBackend((BuggyVision(), second)).interpret(FRAME, FOCUS)
        self.assertEqual(second.calls, [])

    def test_legacy_model_resolution_and_explicit_configured_model(self):
        with patch.dict(os.environ, {"OPENAI_VISION_MODEL": "legacy-vision", "OPENAI_MODEL": "legacy-cognition"},
                        clear=True):
            self.assertEqual(create_visual_perception_backend("openai-responses").model, "legacy-vision")
            self.assertEqual(OpenAIResponsesVisualPerceptionBackend(model="explicit").model, "explicit")
            configured = create_visual_perception_backend("configured", CONFIG)
            self.assertEqual([backend.model for backend in configured.backends], [PRIMARY.model, SECONDARY.model])
            del os.environ["OPENAI_VISION_MODEL"]
            self.assertEqual(create_visual_perception_backend("openai-responses").model, "legacy-cognition")

    async def test_three_entry_chain_attempts_each_once_and_stops_on_secondary_refusal(self):
        backends = [OpenAIResponsesVisualPerceptionBackend(model=SECONDARY.model, client=direct_client(fake))
                    for fake in (FakeCompletions(direct_response(status="incomplete")),
                                 FakeCompletions(direct_response(content=[{"type": "refusal", "refusal": "no"}])),
                                 FakeCompletions(direct_response()))]
        with self.assertRaises(VisualPerceptionError) as caught:
            await OrderedVisualPerceptionBackend(backends).interpret(FRAME, FOCUS)
        self.assertEqual(caught.exception.category, "refused")
        self.assertEqual([len(backend._client.responses.requests) for backend in backends], [1, 1, 0])

    async def test_external_cancellation_propagates_without_switching(self):
        entered = asyncio.Event()
        async def stall():
            entered.set()
            await asyncio.Event().wait()
        backend, first, second, observed = self.chain(stall)
        task = asyncio.create_task(backend.interpret(FRAME, FOCUS))
        await asyncio.wait_for(entered.wait(), .5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(first.requests), 1)
        self.assertEqual(second.requests, [])
        self.assertEqual(observed.snapshot()["provider_calls"][0]["status"], "cancelled")

    async def test_deadlines_and_remaining_sdk_budget_without_retries(self):
        async def stall():
            await asyncio.Event().wait()
        config = replace(CONFIG, request_timeout_seconds=.2,
                         backends=(replace(PRIMARY, request_timeout_seconds=.01), SECONDARY))
        backend, first, second, observed = self.chain(stall, config=config)
        await asyncio.wait_for(backend.interpret(FRAME, FOCUS), .5)
        self.assertEqual((len(first.requests), len(second.requests)), (1, 1))
        self.assertEqual(observed.snapshot()["provider_calls"][0]["failure_category"], "timeout")

        async def consume_some_budget():
            await asyncio.sleep(.02)
            raise ConnectionError()
        backend, first, second, observed = self.chain(consume_some_budget, stall,
                                                      config=replace(CONFIG, request_timeout_seconds=.05))
        with self.assertRaises(VisualPerceptionError) as caught:
            await asyncio.wait_for(backend.interpret(FRAME, FOCUS), .5)
        self.assertEqual(caught.exception.category, "all_backends_failed")
        self.assertEqual((len(first.requests), len(second.requests)), (1, 1))
        self.assertGreater(second.requests[0]["timeout"], 0)
        self.assertLess(second.requests[0]["timeout"], first.requests[0]["timeout"])
        self.assertEqual(observed.snapshot()["provider_calls"][-1]["failure_category"], "timeout")

        backend, first, second, _ = self.chain(stall, config=replace(CONFIG, request_timeout_seconds=.01))
        with self.assertRaises(VisualPerceptionError):
            await asyncio.wait_for(backend.interpret(FRAME, FOCUS), .5)
        self.assertEqual((len(first.requests), len(second.requests)), (1, 0))

    async def test_missing_credentials_and_client_setup_failures_are_not_outbound_attempts(self):
        for failure in ("missing", "connection"):
            with self.subTest(failure=failure), patch.dict(os.environ, {}, clear=True):
                constructor = Mock(side_effect=ConnectionError("private"))
                with patch.dict("sys.modules", {"openai": sdk(constructor)}):
                    if failure == "connection":
                        os.environ[PRIMARY.api_key_env] = "SECRET"
                    second = FakeCompletions(direct_response())
                    backend = create_visual_perception_backend("configured", CONFIG,
                                                               clients=(None, direct_client(second)))
                    observed = RunObservability()
                    backend.observability = observed
                    await backend.interpret(FRAME, FOCUS)
                    self.assertEqual(len(second.requests), 1)
                    self.assertEqual(observed.snapshot()["metrics"]["vision_provider_requests"], 1)
                    self.assertEqual(len(observed.snapshot()["provider_calls"]), 1)
                    failures = [event.error for event in observed.events() if event.operation == "vision_attempt"]
                    self.assertIn("missing_credentials" if failure == "missing" else "connection", failures)
                    if failure == "missing":
                        constructor.assert_not_called()

    async def test_isolated_clients_explicit_keys_endpoints_and_zero_retries(self):
        for entry, returned_client in ((PRIMARY, client(FakeCompletions(response()))),
                                       (SECONDARY, direct_client(FakeCompletions(direct_response())))):
            with self.subTest(backend=entry.backend):
                constructor = Mock(return_value=returned_client)
                with patch.dict(os.environ, {entry.api_key_env: "SECRET", "OPENAI_BASE_URL": "https://other.test",
                    "OPENAI_API_KEY": "OTHER", "OPENAI_VISION_MODEL": "other-vision", "OPENAI_MODEL": "other-model"},
                    clear=True), patch.dict("sys.modules", {"openai": sdk(constructor)}):
                    backend = create_visual_perception_backend("configured", VisionConfiguration(30, (entry,)))
                    with self.assertLogs("embodied_runtime.perception.visual", "INFO") as logs:
                        await backend.interpret(FRAME, FOCUS)
                    self.assertEqual(constructor.call_args.kwargs, {
                        "api_key": "SECRET", "base_url": entry.base_url or "https://api.openai.com/v1",
                        "timeout": 15, "max_retries": 0,
                    })
                    self.assertNotIn("SECRET", "\n".join(logs.output))
                    self.assertNotIn(FOCUS, "\n".join(logs.output))
                    self.assertEqual(os.environ["OPENAI_BASE_URL"], "https://other.test")

    async def test_responses_output_policy_and_local_description_bound(self):
        for value, category in (
            (direct_response("partial", status="incomplete"), "truncated"),
            (direct_response(" "), "empty_response"), ({"status": "completed"}, "malformed_response"),
            (direct_response(content=[{"type": "refusal", "refusal": "private"}]), "refused"),
            (direct_response(status="incomplete", incomplete_details={"reason": "content_filter"}), "refused"),
        ):
            fake = FakeCompletions(value)
            backend = OpenAIResponsesVisualPerceptionBackend(model=SECONDARY.model, client=direct_client(fake))
            with self.subTest(category=category), self.assertRaises(VisualPerceptionError) as caught:
                await backend.interpret(FRAME, FOCUS)
            self.assertEqual(caught.exception.category, category)
        for compatible in (False, True):
            fake = FakeCompletions(response("x" * 2001) if compatible else direct_response("x" * 2001))
            backend = (OpenAICompatibleVisualPerceptionBackend(config=PRIMARY, client=client(fake)) if compatible
                       else OpenAIResponsesVisualPerceptionBackend(model=SECONDARY.model, client=direct_client(fake)))
            result = await backend.interpret(FRAME, FOCUS)
            self.assertEqual(len(result.description), 2000)
            self.assertTrue(result.truncated)

    async def test_failed_response_usage_missing_usage_and_role_pricing_are_separate(self):
        for usage in (None, {}, {"prompt_tokens": 10}):
            backend, _, _, observed = self.chain(response("partial", finish="length", usage=usage))
            observed.provider_call("openai-responses", "gpt-6.1-sol", "initial", usage={
                "input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
                "cached_input_tokens": 0, "cache_write_tokens": 0,
            })
            await backend.interpret(FRAME, FOCUS)
            snapshot = observed.snapshot()
            self.assertEqual(snapshot["metrics"]["provider_requests"], 1)
            self.assertEqual(snapshot["metrics"]["input_tokens"], 10)
            self.assertEqual(snapshot["metrics"]["vision_provider_requests"], 2)
            self.assertIsNone(snapshot["metrics"]["vision_input_tokens"])
            self.assertEqual(len(snapshot["provider_usage"]), 1)
            self.assertEqual(len(snapshot["vision_usage"]), 2)
            self.assertFalse(snapshot["provider_calls"][1]["usage_available"])
        backend, _, _, observed = self.chain(response("partial", finish="length"),
                                             config=replace(CONFIG, backends=(replace(PRIMARY, model=SECONDARY.model), SECONDARY)))
        await backend.interpret(FRAME, FOCUS)
        snapshot = observed.snapshot()
        self.assertEqual(snapshot["metrics"]["vision_input_tokens"], 20)
        self.assertEqual(snapshot["provider_calls"][0]["usage"]["total_tokens"], 13)
        self.assertEqual(snapshot["cost"]["status"], "unavailable")
        self.assertEqual(snapshot["provider_calls"][0]["requested_model"], SECONDARY.model)

    async def test_direct_vision_and_cognition_cost_components_stay_separate(self):
        backend = OpenAIResponsesVisualPerceptionBackend(model=SECONDARY.model,
                        client=direct_client(FakeCompletions(direct_response())))
        observed = RunObservability(pricing=BUILT_IN_PRICING)
        backend.observability = observed
        counts = {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
                  "cached_input_tokens": 0, "cache_write_tokens": 0}
        observed.provider_call("openai-responses", SECONDARY.model, "initial", usage=counts)
        await backend.interpret(FRAME, FOCUS)
        snapshot = observed.snapshot()
        self.assertEqual(snapshot["cost"]["components"]["vision"]["estimated_usd"], "0.000050")
        self.assertEqual(snapshot["cost"]["components"]["cognition"]["estimated_usd"], "0.000050")
        self.assertEqual(snapshot["cost"]["estimated_usd"], "0.000100")
        self.assertEqual(snapshot["metrics"]["provider_requests"], 1)
        self.assertEqual(snapshot["metrics"]["vision_provider_requests"], 1)

    async def test_all_failure_diagnostic_is_bounded_categorized_and_secret_free(self):
        backend, first, second, observed = self.chain(ProviderError(402), ProviderError(404))
        with patch.dict("sys.modules", {"openai": sdk()}), self.assertLogs(
            "embodied_runtime.perception.visual", "INFO") as logs:
            with self.assertRaises(VisualPerceptionError) as caught:
                await backend.interpret(FRAME, FOCUS)
        self.assertEqual(caught.exception.category, "all_backends_failed")
        self.assertLess(len(str(caught.exception)), 300)
        self.assertIn("payment_required,model_unavailable", str(caught.exception))
        serialized = str(caught.exception) + "\n".join(logs.output) + json.dumps(dict(observed.snapshot()))
        for private in ("SECRET", "PRIVATE", FOCUS, "c2FtZS1leGFjdC1mcmFtZQ=="):
            self.assertNotIn(private, serialized)
        self.assertEqual((len(first.requests), len(second.requests)), (1, 1))

    async def test_camera_fallback_preserves_provenance_lease_and_one_acquisition_budget(self):
        captured = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)
        later = datetime(2026, 10, 12, 20, 1, tzinfo=UTC)
        wall = [captured]
        resources = RecordingArbiter()
        async def primary():
            self.assertIsNone(resources.lease_for(CAMERA_RESOURCE))
            wall[0] = later
            raise ConnectionError()
        async def secondary():
            self.assertIsNone(resources.lease_for(CAMERA_RESOURCE))
            return direct_response()
        vision, first, second, _ = self.chain(primary, secondary)
        camera = Camera()
        cognition = SequenceCognition([invoke("observe_scene", {"focus": FOCUS}), no_tool])
        app = RobotApplication(RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True), platform_provider=Platform(), camera_backend=camera,
            cognition_backend=cognition, visual_perception_backend=vision, resource_arbiter=resources,
            wall_clock=lambda: wall[0])
        await app.start()
        try:
            before = app.runtime_state
            app.set_goal("Look carefully")
            await app._request_initiative(visual_helpers.VisualPerceptionTests.stimulus())
            self.assertEqual(camera.captures, 1)
            self.assertEqual(len(resources.acquisitions), 1)
            self.assertIsNone(resources.lease_for(CAMERA_RESOURCE))
            self.assertIs(app.runtime_state, before)
            self.assertEqual(cognition.results[0]["observed_at"], captured.isoformat(timespec="seconds"))
            self.assertIn("observe_scene", cognition.requests[1][2])  # One acquisition slot remains.
            self.assertEqual((len(first.requests), len(second.requests)), (1, 1))
            metrics = app.observability.snapshot()["metrics"]
            self.assertEqual((metrics["camera_captures"], metrics["vision_acquisitions"],
                              metrics["vision_provider_requests"], metrics["vision_fallbacks"]), (1, 1, 2, 1))
        finally:
            await app.stop()

    async def test_runtime_all_failed_chain_uses_bounded_rejected_error_path(self):
        vision, first, second, _ = self.chain(ConnectionError("PRIVATE"), TimeoutError("SECRET"))
        camera = Camera()
        app = RobotApplication(RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=Platform(), camera_backend=camera, visual_perception_backend=vision)
        await app.start()
        try:
            tool_result, interpreted = await app._execute_visual_perception(
                CognitionToolCall("observe_scene", json.dumps({"focus": FOCUS})))
            payload = json.loads(tool_result.output)
            self.assertEqual((payload["status"], payload["failure_category"]), ("rejected", "all_backends_failed"))
            self.assertIsNone(interpreted)
            self.assertLess(len(payload["error"]), 300)
            self.assertNotIn("SECRET", tool_result.output)
            self.assertNotIn("PRIVATE", tool_result.output)
            self.assertEqual((camera.captures, len(first.requests), len(second.requests)), (1, 1, 1))
            self.assertEqual(app.observability.snapshot()["metrics"]["vision_failures"], 1)
        finally:
            await app.stop()
