import asyncio
import os
import json
import sys
import tempfile
from pathlib import Path
from dataclasses import FrozenInstanceError, fields
from types import ModuleType, SimpleNamespace
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import embodied_runtime.app as app_module
from embodied_runtime.app import REMEMBER_TOOL, ApplicationOptions, RobotApplication
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cognition import (
    CognitionContext,
    CognitionError,
    CognitionUnavailableError,
    CognitionToolCall,
    CognitionToolResult,
    TextCognitionBackend,
    compose_cognition_instructions,
)
from embodied_runtime.cognition.openai_responses import (
    DEFAULT_MODEL,
    OpenAIResponsesBackend,
    PREWARM_INPUT,
)
from embodied_runtime.events import ApplicationStarted, EventBus
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile
from embodied_runtime.observability import RunObservability
from embodied_runtime.run_history import RunHistoryEvidenceReader
from embodied_runtime.sensing.camera import CameraBackend, CameraFrame
from embodied_runtime.state import LifecycleState
from embodied_runtime.temporal_context import TemporalContext, TemporalSituation
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


class PreparingCognition(FakeCognition):
    def __init__(self, preparation_error=None):
        super().__init__("later response")
        self.preparation_error = preparation_error
        self.prepare_calls = 0
        self.prepared = False

    async def prepare(self):
        self.prepare_calls += 1
        if self.preparation_error is not None:
            raise self.preparation_error
        self.prepared = True


class BlockingPreparingCognition(FakeCognition):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def prepare(self):
        self.entered.set()
        await self.release.wait()


class MutableBatteryHardware(VirtualHardwareBackend):
    capabilities = ("battery_voltage",)

    def __init__(self, voltage):
        super().__init__()
        self.voltage = voltage

    def read_battery_voltage_v(self):
        if not self.is_running:
            raise RuntimeError("hardware is not running")
        return self.voltage


class CognitionApplicationTests(unittest.IsolatedAsyncioTestCase):
    def make_application(
        self,
        backend=None,
        prompt=None,
        events=None,
        platform=None,
        body=None,
        camera=None,
        hardware=None,
        history=None,
    ):
        return RobotApplication(
            RobotProfile("test", "Test Robot", "A test robot."),
            hardware or VirtualHardwareBackend(),
            ApplicationOptions(startup_prompt=prompt),
            events=events,
            platform_provider=platform or StaticPlatform(),
            body_backend=body,
            camera_backend=camera,
            cognition_backend=backend,
            run_history_evidence=history,
        )

    async def test_default_backend_preparation_is_a_no_op(self):
        backend = FakeCognition()
        self.assertIsNone(await backend.prepare())

    async def test_preparation_precedes_readiness_without_cognition_semantics(self):
        backend = PreparingCognition()
        events = EventBus()
        prepared_at_started = []
        original_publish = events.publish

        async def record_started(event):
            if isinstance(event, ApplicationStarted):
                prepared_at_started.append(backend.prepared)
            await original_publish(event)

        events.publish = record_started
        app = self.make_application(backend, events=events)
        with self.assertLogs("embodied_runtime.app", "INFO") as logs:
            await app.start()
        self.assertEqual(backend.prepare_calls, 1)
        self.assertEqual(prepared_at_started, [True])
        rendered = "\n".join(logs.output)
        self.assertLess(rendered.index("preparation=started"),
                        rendered.index("preparation=ready"))
        self.assertLess(rendered.index("preparation=ready"),
                        rendered.index("[APP] running"))
        self.assertEqual(app.working_memory.snapshot(), ())
        self.assertIsNone(app.active_goal)
        self.assertEqual(backend.requests, [])
        await app.stop()

    async def test_blocked_preparation_remains_starting_before_attention(self):
        backend = BlockingPreparingCognition()
        events = EventBus()
        published = []
        original_publish = events.publish

        async def record(event):
            published.append(event)
            await original_publish(event)

        events.publish = record
        app = self.make_application(backend, events=events)
        attention_start = app.attention.start
        app.attention.start = AsyncMock(side_effect=attention_start)
        with self.assertLogs("embodied_runtime.app", "INFO") as logs:
            start_task = asyncio.create_task(app.start())
            await backend.entered.wait()
            self.assertEqual(app.state, LifecycleState.STARTING)
            app.attention.start.assert_not_awaited()
            self.assertNotIn("[APP] running", "\n".join(logs.output))
            self.assertFalse(any(isinstance(event, ApplicationStarted)
                                 for event in published))
            backend.release.set()
            await start_task
        self.assertEqual(app.state, LifecycleState.RUNNING)
        app.attention.start.assert_awaited_once_with(events)
        self.assertIn("[APP] running", "\n".join(logs.output))
        self.assertTrue(any(isinstance(event, ApplicationStarted)
                            for event in published))
        await app.stop()

    async def test_expected_preparation_failure_is_degraded_and_retry_is_allowed(self):
        backend = PreparingCognition(CognitionUnavailableError("private detail"))
        app = self.make_application(backend)
        with self.assertLogs("embodied_runtime.app", "INFO") as logs:
            await app.start()
        rendered = "\n".join(logs.output)
        self.assertIn("preparation=failed status=degraded", rendered)
        self.assertIn("[APP] running", rendered)
        self.assertNotIn("private detail", rendered)
        self.assertEqual(app.state, LifecycleState.RUNNING)
        self.assertEqual(await app.request_cognition("try normally"), "later response")
        self.assertEqual(len(backend.requests), 1)
        await app.stop()

    async def test_unexpected_preparation_failure_stops_startup(self):
        backend = PreparingCognition(RuntimeError("programming defect"))
        app = self.make_application(backend)
        with self.assertRaisesRegex(RuntimeError, "programming defect"):
            await app.start()
        self.assertEqual(app.state, LifecycleState.STOPPED)
        self.assertFalse(app.hardware.is_running)

    async def test_preparation_cancellation_propagates_and_stops_startup(self):
        backend = PreparingCognition(asyncio.CancelledError())
        app = self.make_application(backend)
        with self.assertRaises(asyncio.CancelledError):
            await app.start()
        self.assertEqual(app.state, LifecycleState.STOPPED)
        self.assertFalse(app.hardware.is_running)

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
        self.assertEqual([tool.name for tool in tools], ["set_goal", "inspect_self"])
        self.assertIsNotNone(executor)
        self.assertIsNotNone(refresh)
        await app.stop()

    async def test_absent_prompt_still_passes_runtime_context(self):
        backend = FakeCognition()
        app = self.make_application(backend)
        await app.start()
        await app.request_cognition("hello")
        self.assertEqual(backend.requests[0][0], "hello")
        self.assertTrue(backend.requests[0][1].startswith("Runtime context\n"))
        await app.stop()

    async def test_power_context_is_backend_neutral_and_refreshed(self):
        backend = FakeCognition()
        hardware = MutableBatteryHardware(7.83)
        app = self.make_application(backend, hardware=hardware)
        await app.start()
        self.assertEqual(app.runtime_state.power.battery_voltage_v, 7.83)
        await app.request_cognition("battery?")
        first = backend.requests[0][1]
        self.assertIn("battery_available: true", first)
        self.assertIn("battery_voltage_v: 7.830", first)
        self.assertNotIn("fusion_hat", first)
        self.assertNotIn("voltage_now", first)
        self.assertNotIn("microvolt", first)

        hardware.voltage = 7.71
        await app.request_cognition("battery now?")
        self.assertIn("battery_voltage_v: 7.710", backend.requests[1][1])
        self.assertEqual(app.runtime_state.power.battery_voltage_v, 7.71)
        await app.stop()

    async def test_virtual_power_context_is_explicitly_unavailable(self):
        backend = FakeCognition()
        app = self.make_application(backend)
        await app.start()
        await app.request_cognition("battery?")
        instructions = backend.requests[0][1]
        self.assertIn("battery_available: false", instructions)
        self.assertIn("battery_voltage_v: unavailable", instructions)
        self.assertIsNone(app.runtime_state.power.battery_voltage_v)
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
        tool = next(tool for tool in app.cognition_tools() if tool.name == "orient_body")
        self.assertEqual(tool.name, "orient_body")
        self.assertEqual(set(tool.parameters["properties"]), {
            "yaw_degrees", "pitch_degrees",
        })
        self.assertEqual(set(tool.parameters["required"]), {
            "yaw_degrees", "pitch_degrees",
        })
        self.assertFalse(tool.parameters["additionalProperties"])
        self.assertEqual(
            [tool.name for tool in self.make_application(FakeCognition()).cognition_tools()],
            ["set_goal", "inspect_self"],
        )

        class NoOrientation(VirtualBodyBackend):
            capabilities = ()

        class PhysicalOrientation(VirtualBodyBackend):
            is_physical = True

        self.assertEqual([tool.name for tool in self.make_application(
            FakeCognition(), body=NoOrientation()).cognition_tools()], ["set_goal", "inspect_self"])
        physical = self.make_application(
            FakeCognition(), body=PhysicalOrientation()
        )
        self.assertEqual([tool.name for tool in physical.cognition_tools()], ["set_goal", "inspect_self"])
        await physical.start()
        result = await physical._execute_cognition_tool(CognitionToolCall(
            "orient_body", '{"yaw_degrees":35,"pitch_degrees":-10}'
        ))
        self.assertEqual(json.loads(result.output)["status"], "rejected")
        self.assertEqual(physical.runtime_state.body.yaw_degrees, 0.0)
        await physical.stop()

    async def test_run_history_is_only_offered_with_injected_read_only_provider(self):
        self.assertNotIn(
            "inspect_run_history",
            [tool.name for tool in self.make_application(FakeCognition()).cognition_tools()],
        )
        with tempfile.TemporaryDirectory() as temporary:
            reader = RunHistoryEvidenceReader(Path(temporary), "R3")
            app = self.make_application(FakeCognition(), history=reader)
            tool = next(tool for tool in app.cognition_tools()
                        if tool.name == "inspect_run_history")
            self.assertEqual(tool.parameters, {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": (
                            "Run selection: recent, current, previous, previous_day, or "
                            "R<positive integer>."
                        ),
                    },
                    "query": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "maxLength": 256,
                        "description": (
                            "Null for metadata/overview; otherwise a literal log search."
                        ),
                    },
                },
                "required": ["selector", "query"],
                "additionalProperties": False,
            })
            self.assertIn("inspect_run_history", app._acquisition_tool_names())
            self.assertEqual(app.acquisition_tools(), ())
            await app.start()
            self.assertNotIn("inspect_run_history",
                             [item.name for item in app.acquisition_tools()])
            app.options = ApplicationOptions(initiative_enabled=True)
            app.set_goal("inspect evidence")
            self.assertIn("inspect_run_history",
                          [item.name for item in app.acquisition_tools()])
            result = await app._execute_cognition_tool(CognitionToolCall(
                "inspect_run_history",
                '{"selector":"recent","query":null}',
            ))
            self.assertEqual(json.loads(result.output)["status"], "applied")
            await app.stop()

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
    async def test_provider_authority_accounts_completion_exactly_once(self):
        response = SimpleNamespace(
            output_text="ok", output=[], id="response", usage=SimpleNamespace(
                input_tokens=15_000, output_tokens=500, total_tokens=15_500,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=12_000, cache_write_tokens=3_000,
                ),
            ))
        observed = RunObservability()
        backend = OpenAIResponsesBackend(
            client=SimpleNamespace(responses=FakeResponses(results=[response])),
            observability=observed,
        )
        self.assertEqual(await backend.respond("hello"), "ok")
        snapshot = observed.snapshot()
        metrics = snapshot["metrics"]
        self.assertEqual((metrics["provider_requests"], metrics["input_tokens"],
                          metrics["cached_input_tokens"],
                          metrics["cache_write_tokens"], metrics["output_tokens"],
                          metrics["total_tokens"]),
                         (1, 15_000, 12_000, 3_000, 500, 15_500))
        self.assertEqual(snapshot["provider_usage"], [{
            "provider": "openai-responses", "model": DEFAULT_MODEL,
            "requests": 1, "input_tokens": 15_000,
            "cached_input_tokens": 12_000, "cache_write_tokens": 3_000,
            "output_tokens": 500, "total_tokens": 15_500,
            "duration_ms": metrics["provider_duration_ms"],
        }])

    async def test_provider_authority_failure_adds_no_usage(self):
        observed = RunObservability()
        backend = OpenAIResponsesBackend(
            client=SimpleNamespace(responses=FakeResponses(error=RuntimeError("no"))),
            observability=observed,
        )
        with self.assertRaises(CognitionError):
            await backend.respond("hello")
        metrics = observed.snapshot()["metrics"]
        self.assertEqual(metrics["provider_failures"], 1)
        self.assertEqual(metrics["provider_requests"], 0)
        self.assertEqual(metrics["total_tokens"], 0)

    async def test_prepare_eagerly_initializes_once_and_prewarm_is_minimal(self):
        responses = FakeResponses(results=[SimpleNamespace(
            output_text="discard me", output=[], id="do-not-retain",
            usage=SimpleNamespace(
                input_tokens=3, output_tokens=1, total_tokens=4,
                input_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )])
        client = SimpleNamespace(responses=responses)
        openai = ModuleType("openai")
        constructions = []

        def construct():
            constructions.append(True)
            return client

        openai.AsyncOpenAI = construct
        backend = OpenAIResponsesBackend(model="configured-model")
        with (
            patch.dict(sys.modules, {"openai": openai}),
            self.assertLogs(
                "embodied_runtime.cognition.openai_responses", "INFO"
            ) as logs,
        ):
            await backend.prepare()
            await backend.prepare()
        self.assertIs(backend._client, client)
        self.assertEqual(constructions, [True])
        self.assertEqual(responses.calls, [{
            "model": "configured-model", "input": PREWARM_INPUT,
        }])
        rendered = "\n".join(logs.output)
        self.assertEqual(rendered.count("component=client_init"), 1)
        self.assertIn(
            "provider_request=prewarm ordinal=1 cold=true status=completed",
            rendered,
        )
        self.assertIn("message_chars=12 instruction_chars=0 tools=0", rendered)
        self.assertIn("cached_input_tokens=0", rendered)
        for excluded in (
            "tools", "instructions", "previous_response_id", "tool_choice",
            "parallel_tool_calls",
        ):
            self.assertNotIn(excluded, responses.calls[0])

    async def test_real_request_after_prewarm_is_second_and_warm(self):
        responses = FakeResponses()
        backend = OpenAIResponsesBackend(client=SimpleNamespace(responses=responses))
        with self.assertLogs(
            "embodied_runtime.cognition.openai_responses", "INFO"
        ) as logs:
            await backend.prepare()
            result = await backend.respond("real operator request", instructions="runtime")
        self.assertEqual(result, "provider text")
        self.assertEqual(responses.calls, [
            {"model": DEFAULT_MODEL, "input": PREWARM_INPUT},
            {"model": DEFAULT_MODEL, "input": "real operator request",
             "instructions": "runtime"},
        ])
        self.assertIn("provider_request=prewarm ordinal=1 cold=true", logs.output[0])
        self.assertIn("provider_request=initial ordinal=2 cold=false", logs.output[1])

    async def test_prepare_client_failure_does_not_attempt_provider(self):
        openai = ModuleType("openai")

        def fail():
            raise RuntimeError("secret configuration")

        openai.AsyncOpenAI = fail
        backend = OpenAIResponsesBackend()
        with patch.dict(sys.modules, {"openai": openai}):
            with self.assertRaises(CognitionUnavailableError) as caught:
                await backend.prepare()
            await backend.prepare()
        self.assertNotIn("secret", str(caught.exception))
        self.assertIsNone(backend._client)
        self.assertEqual(backend._provider_request_ordinal, 0)

    async def test_failed_prewarm_is_not_retried_and_real_request_can_retry(self):
        responses = FakeResponses(RuntimeError("secret provider detail"))
        backend = OpenAIResponsesBackend(client=SimpleNamespace(responses=responses))
        with (
            self.assertLogs(
                "embodied_runtime.cognition.openai_responses", "INFO"
            ) as logs,
            self.assertRaisesRegex(CognitionError, "preparation failed") as caught,
        ):
            await backend.prepare()
        self.assertNotIn("secret", str(caught.exception))
        await backend.prepare()
        self.assertEqual(len(responses.calls), 1)
        self.assertIn(
            "provider_request=prewarm ordinal=1 cold=true status=failed",
            logs.output[0],
        )
        responses.error = None
        self.assertEqual(await backend.respond("later real request"), "provider text")
        self.assertEqual(len(responses.calls), 2)
        self.assertEqual(backend._provider_request_ordinal, 2)

    def test_lazy_client_initialization_is_measured_once(self):
        client = SimpleNamespace(responses=FakeResponses())
        openai = ModuleType("openai")
        openai.AsyncOpenAI = lambda: client
        backend = OpenAIResponsesBackend()
        with (
            patch.dict(sys.modules, {"openai": openai}),
            patch("embodied_runtime.cognition.openai_responses.time.perf_counter",
                  side_effect=[1.0, 1.125]),
            self.assertLogs("embodied_runtime.cognition.openai_responses", "INFO") as logs,
        ):
            self.assertIs(backend._get_client(), client)
            self.assertIs(backend._get_client(), client)
        self.assertEqual(logs.output, [
            "INFO:embodied_runtime.cognition.openai_responses:"
            "[COGNITION] backend=openai-responses component=client_init "
            "status=completed cold=true duration_ms=125"
        ])

    def test_injected_client_emits_no_client_initialization_measurement(self):
        backend = OpenAIResponsesBackend(client=object())
        with self.assertNoLogs("embodied_runtime.cognition.openai_responses", "INFO"):
            backend._get_client()

    def test_client_initialization_failure_is_bounded_and_preserved(self):
        openai = ModuleType("openai")

        def fail():
            raise RuntimeError("secret client configuration")

        openai.AsyncOpenAI = fail
        backend = OpenAIResponsesBackend()
        with (
            patch.dict(sys.modules, {"openai": openai}),
            patch("embodied_runtime.cognition.openai_responses.time.perf_counter",
                  side_effect=[2.0, 2.02]),
            self.assertLogs("embodied_runtime.cognition.openai_responses", "INFO") as logs,
            self.assertRaises(CognitionError) as caught,
        ):
            backend._get_client()
        self.assertIsInstance(caught.exception, CognitionUnavailableError)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(logs.output, [
            "INFO:embodied_runtime.cognition.openai_responses:"
            "[COGNITION] backend=openai-responses component=client_init "
            "status=failed cold=true duration_ms=20"
        ])

    def test_all_provider_tools_have_compatible_strict_object_schemas(self):
        tool_definitions = (
            value for value in vars(app_module).values()
            if isinstance(value, app_module.CognitionToolDefinition)
        )
        for tool in tool_definitions:
            with self.subTest(tool=tool.name):
                provider_tool = OpenAIResponsesBackend._provider_tool(tool)
                parameters = provider_tool["parameters"]
                self.assertTrue(provider_tool["strict"])
                self.assertIs(parameters["additionalProperties"], False)
                self.assertEqual(
                    set(parameters["required"]), set(parameters["properties"])
                )

        properties = REMEMBER_TOOL.parameters["properties"]
        self.assertEqual(properties["related_entity"]["type"], ["string", "null"])
        self.assertEqual(properties["related_role"]["type"], ["string", "null"])
        self.assertIn("canonical name or exact alias", properties["subject"]["description"])
        for reference in ("you", "your", "yours", "yourself"):
            self.assertIn(reference, REMEMBER_TOOL.description)
        self.assertIn("I, me, my, mine, and myself", REMEMBER_TOOL.description)
        self.assertIn("bounded runtime-self reference", properties["subject"]["description"])
        self.assertIn("simple machine identifier", properties["predicate"]["description"])
        self.assertIn("CURRENT operator utterance", properties["evidence"]["description"])
        self.assertIn("Null for fact/preference", properties["related_entity"]["description"])

    async def test_remember_provider_request_uses_strict_nullable_schema(self):
        responses = FakeResponses()
        backend = OpenAIResponsesBackend(client=SimpleNamespace(responses=responses))

        await backend.respond(
            "remember this", tools=(REMEMBER_TOOL,),
            tool_executor=lambda call: None,
            refreshed_instructions=lambda: "fresh",
        )

        provider_tool = responses.calls[0]["tools"][0]
        parameters = provider_tool["parameters"]
        self.assertTrue(provider_tool["strict"])
        self.assertIs(parameters["additionalProperties"], False)
        self.assertEqual(set(parameters["required"]), set(parameters["properties"]))
        self.assertEqual(
            parameters["properties"]["related_entity"]["type"], ["string", "null"]
        )
        self.assertEqual(
            parameters["properties"]["related_role"]["type"], ["string", "null"]
        )

    def test_provider_tool_rejects_incompatible_strict_schema(self):
        invalid = app_module.CognitionToolDefinition(
            name="invalid", description="invalid test tool",
            parameters={
                "type": "object", "properties": {"value": {"type": "string"}},
                "required": [], "additionalProperties": False,
            },
        )
        with self.assertRaisesRegex(CognitionError, "invalid"):
            OpenAIResponsesBackend._provider_tool(invalid)

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

    async def test_initial_provider_timings_advance_and_omit_content(self):
        responses = FakeResponses()
        backend = OpenAIResponsesBackend(client=SimpleNamespace(responses=responses))
        with (
            patch("embodied_runtime.cognition.openai_responses.time.perf_counter",
                  side_effect=[1.0, 1.25, 2.0, 2.5]),
            self.assertLogs("embodied_runtime.cognition.openai_responses", "INFO") as logs,
        ):
            await backend.respond("secret first", instructions="private")
            await backend.respond("hidden", instructions="grounding")
        self.assertIn(
            "provider_request=initial ordinal=1 cold=true status=completed "
            "duration_ms=250 message_chars=12 instruction_chars=7 tools=0",
            logs.output[0],
        )
        self.assertIn(
            "provider_request=initial ordinal=2 cold=false status=completed "
            "duration_ms=500 message_chars=6 instruction_chars=9 tools=0",
            logs.output[1],
        )
        self.assertNotIn("secret first", " ".join(logs.output))
        self.assertNotIn("private", " ".join(logs.output))

    async def test_public_usage_metadata_is_logged_and_absence_is_harmless(self):
        usage = SimpleNamespace(
            input_tokens=100, output_tokens=7, total_tokens=107,
            input_tokens_details=SimpleNamespace(
                cached_tokens=80, cache_write_tokens=12,
            ),
        )
        responses = FakeResponses(results=[
            SimpleNamespace(output_text="one", output=[], id="one", usage=usage),
            SimpleNamespace(output_text="two", output=[], id="two"),
        ])
        backend = OpenAIResponsesBackend(client=SimpleNamespace(responses=responses))
        with self.assertLogs(
            "embodied_runtime.cognition.openai_responses", "INFO"
        ) as logs:
            await backend.respond("first")
            await backend.respond("second")
        self.assertIn(
            "input_tokens=100 output_tokens=7 total_tokens=107 "
            "cached_input_tokens=80 cache_write_tokens=12", logs.output[0],
        )
        self.assertNotIn("input_tokens=", logs.output[1])

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
        with (
            patch("embodied_runtime.cognition.openai_responses.time.perf_counter",
                  side_effect=[0.0, 1.0, 100.0, 101.0]),
            self.assertLogs("embodied_runtime.cognition.openai_responses", "INFO") as logs,
        ):
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
        self.assertIn(
            "provider_request=initial ordinal=1 cold=true status=completed "
            "duration_ms=1000 message_chars=4 instruction_chars=6 tools=1",
            logs.output[0],
        )
        self.assertIn(
            "provider_request=continuation ordinal=2 cold=false status=completed "
            "duration_ms=1000 instruction_chars=5 tools=0", logs.output[1],
        )

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
        with (
            self.assertLogs("embodied_runtime.cognition.openai_responses", "INFO") as logs,
            self.assertRaisesRegex(CognitionError, "OpenAI Responses request failed") as caught,
        ):
            await backend.respond("sensitive message")
        self.assertNotIn("secret detail", str(caught.exception))
        self.assertIn(
            "provider_request=initial ordinal=1 cold=true status=failed",
            logs.output[0],
        )
        self.assertNotIn("secret detail", logs.output[0])
        self.assertNotIn("sensitive message", logs.output[0])

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
        self.assertEqual(len(names), 30)
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
        temporal = TemporalContext(datetime(2026, 9, 10, tzinfo=UTC), "UTC")
        situation = TemporalSituation(None, None, "none", None, None, None, None, None)
        composed = compose_cognition_instructions(
            self.make_context(), temporal, situation, prompt
        )
        self.assertIn(prompt, composed)
        self.assertLess(composed.index("Operator instructions"), composed.index(prompt))
        self.assertLess(composed.index(prompt), composed.index("Runtime context"))
        self.assertLess(composed.index("Runtime context"),
                        composed.index("Temporal context"))
        self.assertLess(composed.index("Temporal context"),
                        composed.index("Temporal situation"))
