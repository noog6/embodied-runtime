"""Offline portability checks against the adapter and the production Job harness."""

import asyncio
from contextlib import redirect_stderr
from copy import deepcopy
from dataclasses import replace
import json
from io import StringIO
from itertools import product
import os
from pathlib import Path
import sys
import tempfile
import traceback
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from embodied_runtime.attachments import ImageAttachment
from embodied_runtime.benchmarks import run_trial
from embodied_runtime.benchmarks.__main__ import _arguments, build_benchmark_backend
from embodied_runtime.benchmarks.scenario import SCENARIOS, FRESH_RUNTIME_SCENARIO_ID
from embodied_runtime.cli import build_cognition_backend, parse_launch_arguments, validate_launch_dependencies
from embodied_runtime.cognition import CognitionError, CognitionToolDefinition, CognitionToolResult
from embodied_runtime.cognition.factory import create_cognition_backend
from embodied_runtime.cognition.openai_compatible import OpenAICompatibleBackend
from embodied_runtime.cognition.openai_responses import OpenAIResponsesBackend
from embodied_runtime.config import (
    ConfigurationError, OpenAICompatibleConfig, load_runtime_config,
    validate_compatible_config,
)
from embodied_runtime.observability import RunObservability
from embodied_runtime.pricing import BUILT_IN_PRICING


CONFIG = OpenAICompatibleConfig(
    model="explicit/tool-model", base_url="https://openrouter.ai/api/v1",
    api_key_env="TEST_ROUTER_KEY", tool_calling=True,
)
TOML = '''
[runtime]
cognition = "openai-compatible"
[cognition.openai_compatible]
model = "explicit/tool-model"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "TEST_ROUTER_KEY"
request_timeout_seconds = 12
tool_calling = true
image_input = false
'''
TOOL = CognitionToolDefinition("effect", "One effect", {
    "type": "object", "properties": {}, "required": [], "additionalProperties": False,
})
USAGE = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
         "prompt_tokens_details": {"cached_tokens": 2}}


def call(name="effect", arguments="{}", call_id="call-exact"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def response(content="answer", *, calls=None, finish=None, usage=USAGE, **fields):
    return {"model": "returned/model-id", "usage": deepcopy(usage), "choices": [{
        "index": 0,
        "finish_reason": finish or ("tool_calls" if calls else "stop"),
        "message": {"role": "assistant", "content": content, "tool_calls": calls, **fields},
    }]}


class FakeCompletions:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    async def create(self, **arguments):
        self.requests.append(deepcopy(arguments))
        value = self.results.pop(0)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return await value()
        return value


def client(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


class CompatibleConfigurationTests(unittest.TestCase):
    def file(self, contents=TOML):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(contents)
        return path

    def test_runtime_and_benchmark_share_construction_and_overrides(self):
        path = self.file()
        _, runtime, effective = parse_launch_arguments([
            "--config", str(path), "--cognition-model", "override/tool-model",
            "--compatible-request-timeout-seconds", "8", "--compatible-image-input",
        ])
        benchmark = _arguments([
            "--config", str(path), "--model", "override/tool-model",
            "--compatible-request-timeout-seconds", "8", "--compatible-image-input",
        ])
        validate_launch_dependencies(runtime)
        first = build_cognition_backend(runtime)
        second = build_benchmark_backend(benchmark, benchmark.model[0])
        self.assertIsInstance(first, OpenAICompatibleBackend)
        self.assertEqual(first.config, second.config)
        self.assertEqual(first.config.model, effective.cognition_model)
        self.assertEqual(first.config.request_timeout_seconds, 8)
        self.assertTrue(first.supports_image_input)

    def test_existing_default_and_model_environment_precedence(self):
        with patch.dict(os.environ, {"OPENAI_MODEL": "environment-model"}, clear=True):
            _, args, _ = parse_launch_arguments(["--cognition", "openai-responses"])
            self.assertIsInstance(build_cognition_backend(args), OpenAIResponsesBackend)
            self.assertEqual(build_cognition_backend(args).model, "environment-model")
            self.assertIsNone(build_cognition_backend(parse_launch_arguments([])[1]))
            benchmark = _arguments(["--model", "explicit-model"])
            self.assertIsInstance(build_benchmark_backend(benchmark, benchmark.model[0]), OpenAIResponsesBackend)
            self.assertEqual(build_benchmark_backend(benchmark, benchmark.model[0]).model, "explicit-model")
            compatible = parse_launch_arguments(["--config", str(self.file())])[1]
            self.assertEqual(build_cognition_backend(compatible).model, CONFIG.model)

    def test_compatible_model_never_defaults_to_openai_environment(self):
        with patch.dict(os.environ, {"OPENAI_MODEL": "environment-model"}, clear=True):
            for key in ("model", "base_url", "api_key_env", "tool_calling"):
                with self.subTest(key=key):
                    text = "\n".join(line for line in TOML.splitlines() if not line.startswith(key + " ="))
                    args = parse_launch_arguments(["--config", str(self.file(text))])[1]
                    with self.assertRaises(ConfigurationError):
                        validate_launch_dependencies(args)

    def test_configuration_rejects_invalid_fields_without_echoing_secrets(self):
        for field, value in (
            ("model", ""), ("model", 1), ("api_key_env", "secret-key-value!"),
            ("base_url", "https://user:SECRET@example.test/api"),
            ("base_url", "https://example.test/api?api_key=SECRET"),
            ("base_url", "https://example.test/api#SECRET"),
            ("base_url", "https://example.test:bad/api"), ("base_url", "ftp://example.test"),
            ("request_timeout_seconds", 0), ("request_timeout_seconds", -1),
            ("request_timeout_seconds", float("inf")), ("request_timeout_seconds", float("nan")),
            ("request_timeout_seconds", True), ("tool_calling", False),
            ("tool_calling", "true"), ("image_input", 1),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ConfigurationError) as caught:
                validate_compatible_config(replace(CONFIG, **{field: value}))
            self.assertNotIn("SECRET", str(caught.exception))
        for text in (TOML + 'api_key = "SECRET"\n', TOML + 'unknown = true\n',
                     '[cognition]\nopenai_compatible = "SECRET"\n'):
            with self.assertRaises(ConfigurationError) as caught:
                load_runtime_config(self.file(text))
            self.assertNotIn("SECRET", str(caught.exception))

    def test_validation_does_not_need_credentials_or_provider_calls(self):
        with patch.dict(os.environ, {}, clear=True):
            args = parse_launch_arguments(["--config", str(self.file())])[1]
            validate_launch_dependencies(args)
            backend = build_cognition_backend(args)
            self.assertIsNone(backend._client)
            self.assertEqual(create_cognition_backend("openai-compatible", compatible=CONFIG).config, CONFIG)

    def test_cli_only_configuration_is_shared(self):
        flags = ["--compatible-base-url", CONFIG.base_url, "--compatible-api-key-env", CONFIG.api_key_env,
                 "--compatible-tool-calling", "--compatible-request-timeout-seconds", "5"]
        runtime = parse_launch_arguments([
            "--cognition", "openai-compatible", "--cognition-model", CONFIG.model, *flags,
        ])[1]
        benchmark = _arguments(["--backend", "openai-compatible", "--model", CONFIG.model, *flags])
        self.assertEqual(build_cognition_backend(runtime).config,
                         build_benchmark_backend(benchmark, CONFIG.model).config)

    def test_parallel_tool_calls_mode_defaults_parsing_and_cli_precedence(self):
        self.assertEqual(CONFIG.parallel_tool_calls_mode, "disable")
        for configured in (None, "disable", "omit"):
            text = TOML + (f'parallel_tool_calls_mode = "{configured}"\n' if configured else "")
            path = self.file(text)
            self.assertEqual(load_runtime_config(path).cognition.openai_compatible.parallel_tool_calls_mode,
                             configured or "disable")
            for override in (None, "disable", "omit"):
                with self.subTest(configured=configured, override=override):
                    flags = ["--config", str(path)]
                    if override is not None:
                        flags.extend(["--compatible-parallel-tool-calls-mode", override])
                    _, runtime, effective = parse_launch_arguments(flags)
                    validate_launch_dependencies(runtime)
                    benchmark = _arguments(flags)
                    first = build_cognition_backend(runtime)
                    second = build_benchmark_backend(benchmark, benchmark.model[0])
                    self.assertEqual(first.config, second.config)
                    self.assertEqual(first.config, effective.cognition_openai_compatible)
                    self.assertEqual(first.config.parallel_tool_calls_mode, override or configured or "disable")
                    self.assertIsNone(first._client)
                    self.assertIsNone(second._client)

    def test_parallel_tool_calls_mode_rejects_invalid_config_and_cli_values(self):
        message = "parallel_tool_calls_mode must be 'disable' or 'omit'"
        for value in ("", "enable", "true", "Disable", " omit ", True, 1, None, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigurationError, message):
                validate_compatible_config(replace(CONFIG, parallel_tool_calls_mode=value))
        for literal in ('""', '"enable"', '"true"', 'true', '1', '[]', '{}'):
            with self.subTest(toml=literal), self.assertRaisesRegex(ConfigurationError, message):
                load_runtime_config(self.file(TOML + f"parallel_tool_calls_mode = {literal}\n"))
        for parse in (parse_launch_arguments, _arguments):
            error = StringIO()
            with self.subTest(parser=parse.__name__), redirect_stderr(error), self.assertRaises(SystemExit):
                parse(["--compatible-parallel-tool-calls-mode", "enable"])
            self.assertIn("--compatible-parallel-tool-calls-mode", error.getvalue())
            self.assertIn("invalid choice", error.getvalue())
            self.assertIn("{disable,omit}", error.getvalue())

    def test_benchmark_requires_explicit_or_configured_model(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            _arguments([])
        args = _arguments(["--config", str(self.file())])
        self.assertEqual(args.model, [CONFIG.model])


class CompatibleAdapterTests(unittest.IsolatedAsyncioTestCase):
    def make(self, *results, config=CONFIG):
        fake = FakeCompletions(*results)
        observed = RunObservability(pricing=BUILT_IN_PRICING)
        return OpenAICompatibleBackend(config=config, client=client(fake), observability=observed), fake, observed

    async def tool_request(self, backend, executor=None, **kwargs):
        executor = executor or AsyncMock(return_value=CognitionToolResult('{"status":"applied"}'))
        value = await backend.respond("original request", instructions="old authority", tools=(TOOL,),
                                      tool_executor=executor, refreshed_instructions=lambda: "fresh authority", **kwargs)
        return value, executor

    async def test_plain_text_and_diagnostics(self):
        backend, fake, observed = self.make(response())
        self.assertEqual(await backend.respond("hello", instructions="current authority"), "answer")
        self.assertEqual(fake.requests[0]["messages"], [
            {"role": "system", "content": "current authority"}, {"role": "user", "content": "hello"},
        ])
        self.assertFalse(fake.requests[0]["stream"])
        self.assertEqual(fake.requests[0]["extra_body"], {"provider": {
            "require_parameters": True, "allow_fallbacks": False,
        }})
        record = observed.snapshot()["provider_calls"][0]
        self.assertEqual(record["requested_model"], CONFIG.model)
        self.assertEqual(record["returned_model"], "returned/model-id")
        self.assertEqual(record["protocol"], "chat-completions")
        self.assertEqual(record["endpoint"], "https://openrouter.ai")
        self.assertEqual(record["status"], "completed")
        self.assertIsInstance(record["duration_ms"], int)
        self.assertEqual(record["usage"]["cached_input_tokens"], 2)
        self.assertIsNone(observed.snapshot()["cost"]["estimated_usd"])

    async def test_exactly_one_execution_and_request_local_continuation(self):
        reasoning = [{"type": "reasoning.encrypted", "data": "opaque-signature", "index": 0}]
        backend, fake, observed = self.make(response(None, calls=[call()], reasoning_details=reasoning), response("final"))
        value, executor = await self.tool_request(backend)
        self.assertEqual(value, "final")
        executor.assert_awaited_once()
        self.assertEqual(executor.await_args.args[0].arguments, "{}")
        initial, final = fake.requests
        self.assertEqual(initial["messages"][0]["content"], "old authority")
        self.assertEqual(final["messages"], [
            {"role": "system", "content": "fresh authority"},
            {"role": "user", "content": "original request"},
            {"role": "assistant", "content": None, "tool_calls": [call()], "reasoning_details": reasoning},
            {"role": "tool", "tool_call_id": "call-exact", "content": '{"status":"applied"}'},
        ])
        self.assertEqual(final["tool_choice"], "none")
        self.assertEqual(final["tools"], initial["tools"])
        self.assertFalse(initial["parallel_tool_calls"])
        self.assertFalse(final["parallel_tool_calls"])
        self.assertEqual(initial["tool_choice"], "auto")
        self.assertNotIn("previous_response_id", final)
        self.assertNotIn("opaque-signature", json.dumps(dict(observed.snapshot())))
        self.assertNotIn("messages", backend.__dict__)
        # A second respond starts independently, with no prior tool-result state.
        fake.results.append(response("independent"))
        await backend.respond("new request")
        self.assertEqual(fake.requests[-1]["messages"], [{"role": "user", "content": "new request"}])
        self.assertNotIn("parallel_tool_calls", fake.requests[-1])

    async def test_omit_changes_only_parallel_parameter_on_both_tool_requests(self):
        requests = {}
        for mode in ("disable", "omit"):
            backend, fake, _ = self.make(response(None, calls=[call()]), response("final"),
                                         config=replace(CONFIG, parallel_tool_calls_mode=mode))
            value, executor = await self.tool_request(backend)
            self.assertEqual(value, "final")
            executor.assert_awaited_once()
            self.assertEqual(len(fake.requests), 2)
            requests[mode] = fake.requests
        for disabled, omitted in zip(requests["disable"], requests["omit"], strict=True):
            self.assertIs(disabled.pop("parallel_tool_calls"), False)
            self.assertNotIn("parallel_tool_calls", omitted)
            self.assertEqual(omitted, disabled)
            self.assertEqual(omitted["extra_body"]["provider"], {
                "require_parameters": True, "allow_fallbacks": False,
            })
        self.assertEqual(requests["omit"][0]["tool_choice"], "auto")
        self.assertEqual(requests["omit"][1]["tool_choice"], "none")

    async def test_multiple_calls_in_omit_mode_execute_no_tools(self):
        backend, fake, _ = self.make(response(None, calls=[call(), call(call_id="another")]),
                                     config=replace(CONFIG, parallel_tool_calls_mode="omit"))
        executor = AsyncMock()
        with self.assertRaises(CognitionError) as caught:
            await self.tool_request(backend, executor)
        self.assertEqual(caught.exception.category, "multiple_tool_calls")
        executor.assert_not_awaited()
        self.assertEqual(len(fake.requests), 1)
        self.assertNotIn("parallel_tool_calls", fake.requests[0])

    async def test_sdk_response_objects_preserve_openrouter_extra_fields(self):
        try:
            from openai.types.chat import ChatCompletion
        except ImportError:
            self.skipTest("openai optional dependency is unavailable")
        reasoning = [{"type": "reasoning.text", "text": "private reasoning", "signature": "opaque"}]
        values = [response(None, calls=[call()], reasoning_details=reasoning), response("final")]
        results = [ChatCompletion.model_validate({
            **value, "id": "completion", "created": 1, "object": "chat.completion",
        }) for value in values]
        backend, fake, observed = self.make(*results)
        self.assertEqual((await self.tool_request(backend))[0], "final")
        self.assertEqual(fake.requests[1]["messages"][2]["reasoning_details"], reasoning)
        self.assertNotIn("private reasoning", json.dumps(dict(observed.snapshot())))

    async def test_multiple_and_malformed_calls_execute_no_effects(self):
        invalid = [
            response(None, calls=[call(), call(call_id="another")]),
            response(None, calls=[{**call(), "type": "custom"}]),
            response(None, calls=[{**call(), "id": None}]),
            response(None, calls=[{**call(), "function": None}]),
            response(None, calls=[call(arguments="not-json")]),
            response(None, calls=[call(arguments="[]")]),
            response(None, calls=[call(arguments='{"value":NaN}')]),
            response(None, calls=[call(arguments={})]),
            response(None, calls=[call(name="")]),
            response(None, calls=[call()], finish="stop"),
            response(None, calls="malformed", finish="tool_calls"),
            response(None, calls=[call()], reasoning_details={"invalid": True}),
        ]
        for result in invalid:
            with self.subTest(result=result):
                backend, fake, _ = self.make(result)
                executor = AsyncMock()
                with self.assertRaises(CognitionError):
                    await self.tool_request(backend, executor)
                executor.assert_not_awaited()
                self.assertEqual(len(fake.requests), 1)

    async def test_no_tools_and_final_tool_requests_are_rejected(self):
        for mode in ("disable", "omit"):
            with self.subTest(mode=mode):
                config = replace(CONFIG, parallel_tool_calls_mode=mode)
                backend, _, _ = self.make(response(None, calls=[call()]), config=config)
                with self.assertRaises(CognitionError):
                    await backend.respond("unoffered")
                backend, fake, _ = self.make(response(None, calls=[call()]), response(None, calls=[call()]),
                                             config=config)
                executor = AsyncMock(return_value=CognitionToolResult("{}"))
                with self.assertRaises(CognitionError) as caught:
                    await self.tool_request(backend, executor)
                self.assertEqual(caught.exception.category, "unexpected_tool_call")
                executor.assert_awaited_once()
                self.assertEqual(len(fake.requests), 2)
                self.assertEqual(fake.requests[-1]["tool_choice"], "none")

    async def test_empty_refused_truncated_and_malformed_response_categories(self):
        for result, category in (
            (response(" "), "empty_response"), (response(None), "empty_response"),
            (response("partial", finish="length"), "truncated"),
            (response("no", refusal="refused"), "refused"),
            (response(None, finish="content_filter"), "refused"),
            ({"choices": []}, "malformed_response"),
            (response([{"type": "text", "text": "no"}]), "malformed_response"),
            (response("no", function_call={"name": "legacy"}), "malformed_response"),
            (response("no", finish="function_call"), "malformed_response"),
            (response("no", finish="tool_calls"), "malformed_tool_call"),
        ):
            with self.subTest(category=category):
                backend, _, observed = self.make(result)
                with self.assertRaises(CognitionError) as caught:
                    await backend.respond("test")
                self.assertEqual(caught.exception.category, category)
                self.assertEqual(observed.snapshot()["provider_calls"][0]["failure_category"], category)

    async def test_timeouts_cancellation_and_continuation_failure_never_repeat_effect(self):
        async def stall():
            await asyncio.Event().wait()
        for mode in ("disable", "omit"):
            for failure, category in (
                (TimeoutError("secret timeout"), "timeout"),
                (RuntimeError("secret failure"), "provider_error"),
                (asyncio.CancelledError(), "cancelled"), (stall, "timeout"),
            ):
                with self.subTest(mode=mode, category=category):
                    backend, fake, observed = self.make(response(None, calls=[call()]), failure,
                        config=replace(CONFIG, request_timeout_seconds=.01, parallel_tool_calls_mode=mode))
                    executor = AsyncMock(return_value=CognitionToolResult("{}"))
                    with self.assertRaises(asyncio.CancelledError if category == "cancelled" else CognitionError):
                        await self.tool_request(backend, executor)
                    executor.assert_awaited_once()
                    self.assertEqual(len(fake.requests), 2)
                    self.assertEqual(observed.snapshot()["provider_calls"][-1]["failure_category"], category)

    async def test_initial_timeout_and_cancellation_execute_no_effect(self):
        for failure in (TimeoutError(), asyncio.CancelledError()):
            backend, fake, _ = self.make(failure)
            executor = AsyncMock()
            with self.assertRaises(type(failure) if isinstance(failure, asyncio.CancelledError) else CognitionError):
                await self.tool_request(backend, executor)
            executor.assert_not_awaited()
            self.assertEqual(len(fake.requests), 1)

    async def test_external_task_cancellation_propagates_after_execution(self):
        entered = asyncio.Event()
        async def stall():
            entered.set()
            await asyncio.Event().wait()
        backend, fake, observed = self.make(response(None, calls=[call()]), stall)
        executor = AsyncMock(return_value=CognitionToolResult("{}"))
        task = asyncio.create_task(self.tool_request(backend, executor))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        executor.assert_awaited_once()
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(observed.snapshot()["provider_calls"][-1]["status"], "cancelled")

    async def test_executor_failure_is_not_retried(self):
        backend, fake, _ = self.make(response(None, calls=[call()]))
        executor = AsyncMock(side_effect=RuntimeError("authority expired"))
        with self.assertRaisesRegex(RuntimeError, "authority expired"):
            await self.tool_request(backend, executor)
        executor.assert_awaited_once()
        self.assertEqual(len(fake.requests), 1)

    async def test_images_require_explicit_capability_and_survive_continuation(self):
        image = ImageAttachment("image/jpeg", b"image")
        backend, fake, _ = self.make(response())
        with self.assertRaisesRegex(CognitionError, "does not support image"):
            await backend.respond("caption", image_attachments=(image,))
        self.assertEqual(fake.requests, [])
        backend, fake, _ = self.make(response(None, calls=[call()]), response(),
                                  config=replace(CONFIG, image_input=True))
        await self.tool_request(backend, image_attachments=(image,))
        user = fake.requests[0]["messages"][1]
        self.assertEqual(user["content"][1]["image_url"]["url"], "data:image/jpeg;base64,aW1hZ2U=")
        self.assertEqual(fake.requests[1]["messages"][1], user)
        with self.assertRaisesRegex(CognitionError, "at most one"):
            await backend.respond("caption", image_attachments=(image, image))
        self.assertEqual(len(fake.requests), 2)

    async def test_missing_partial_and_invalid_usage_stays_unavailable(self):
        for usage in (None, {}, {"prompt_tokens": 5}, {**USAGE, "completion_tokens": True},
                      {**USAGE, "prompt_tokens": -1}):
            backend, _, observed = self.make(response(usage=usage), config=replace(CONFIG, model="gpt-6.1-sol"))
            await backend.respond("test")
            snapshot = observed.snapshot()
            self.assertIsNone(snapshot["metrics"]["input_tokens"])
            self.assertFalse(snapshot["provider_usage"][0]["usage_available"])
            self.assertIsNone(snapshot["provider_usage"][0]["output_tokens"])
            self.assertIsNone(snapshot["cost"]["estimated_usd"])
            self.assertFalse(snapshot["provider_calls"][0]["usage_available"])
        backend, _, observed = self.make(response(), config=replace(CONFIG, model="gpt-6.1-sol"))
        await backend.respond("test")
        self.assertTrue(observed.snapshot()["provider_calls"][0]["usage_available"])
        self.assertIsNone(observed.snapshot()["cost"]["estimated_usd"])

    async def test_responses_missing_usage_remains_unavailable_in_shared_diagnostics(self):
        for usage in (None, SimpleNamespace(input_tokens=10),
                      SimpleNamespace(input_tokens=-1, output_tokens=1, total_tokens=0)):
            observed = RunObservability(pricing=BUILT_IN_PRICING)
            response_value = SimpleNamespace(output=[], output_text="answer", model="returned/model", usage=usage)
            backend = OpenAIResponsesBackend(client=SimpleNamespace(
                responses=SimpleNamespace(create=AsyncMock(return_value=response_value)),
            ), observability=observed)
            with self.assertLogs("embodied_runtime.cognition.openai_responses", "INFO") as logs:
                self.assertEqual(await backend.respond("test"), "answer")
            self.assertIn("usage_available=false", "\n".join(logs.output))
            self.assertIsNone(observed.snapshot()["metrics"]["input_tokens"])
            self.assertIsNone(observed.snapshot()["cost"]["estimated_usd"])
            self.assertEqual(observed.snapshot()["provider_calls"][0]["protocol"], "responses")

    async def test_sdk_connection_is_isolated_bounded_and_secret_safe(self):
        for mode in ("disable", "omit"):
            with self.subTest(mode=mode):
                fake = FakeCompletions(response())
                module = ModuleType("openai")
                constructor = module.AsyncOpenAI = Mock(return_value=client(fake))
                backend = OpenAICompatibleBackend(config=replace(CONFIG, parallel_tool_calls_mode=mode))
                with patch.dict(sys.modules, {"openai": module}), patch.dict(os.environ, {
                    "TEST_ROUTER_KEY": "SECRET", "OPENAI_API_KEY": "OTHER-SECRET",
                    "OPENAI_BASE_URL": "https://other.test", "OPENAI_MODEL": "other-model",
                }), self.assertLogs("embodied_runtime.cognition.openai_compatible", "INFO") as logs:
                    await backend.respond("private prompt")
                self.assertEqual(constructor.call_args.kwargs, {
                    "api_key": "SECRET", "base_url": CONFIG.base_url, "timeout": 30, "max_retries": 0,
                })
                self.assertNotIn("SECRET", "\n".join(logs.output))
                self.assertNotIn("private prompt", "\n".join(logs.output))
        fake = FakeCompletions(RuntimeError("Authorization Bearer SECRET https://user:SECRET@host?key=SECRET"))
        backend = OpenAICompatibleBackend(config=CONFIG, client=client(fake), observability=RunObservability())
        with self.assertLogs("embodied_runtime.cognition.openai_compatible", "INFO") as logs:
            try:
                await backend.respond("test")
            except CognitionError as caught:
                rendered = "".join(traceback.format_exception(caught))
        self.assertNotIn("SECRET", rendered + "\n".join(logs.output) + json.dumps(dict(backend.observability.snapshot())))

    async def test_missing_credentials_and_prepare_readiness(self):
        module = ModuleType("openai")
        module.AsyncOpenAI = Mock()
        with patch.dict(sys.modules, {"openai": module}), patch.dict(os.environ, {}, clear=True):
            observed = RunObservability()
            backend = OpenAICompatibleBackend(config=CONFIG, observability=observed)
            with self.assertRaisesRegex(CognitionError, "credential environment variable"):
                await backend.prepare()
            module.AsyncOpenAI.assert_not_called()
            self.assertEqual(observed.snapshot()["metrics"]["provider_requests"], 0)
            self.assertEqual(observed.snapshot()["provider_calls"], [])
        for mode in ("disable", "omit"):
            with self.subTest(mode=mode):
                backend, fake, _ = self.make(response(), config=replace(CONFIG, parallel_tool_calls_mode=mode))
                await backend.prepare()
                await backend.prepare()
                self.assertEqual(len(fake.requests), 1)
                self.assertEqual(fake.requests[0]["messages"], [{"role": "user", "content": "Reply ready."}])
                self.assertNotIn("tools", fake.requests[0])
                self.assertNotIn("parallel_tool_calls", fake.requests[0])
        failed, _, _ = self.make(response("", finish="length"))
        with self.assertRaises(CognitionError):
            await failed.prepare()


class CompatibleBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_six_unchanged_scenarios_use_the_production_harness(self):
        for mode, scenario_id in product(("disable", "omit"), SCENARIOS):
            with self.subTest(mode=mode, scenario=scenario_id):
                actions = []
                if scenario_id == FRESH_RUNTIME_SCENARIO_ID:
                    actions.extend([
                        ("workspace_read", {"path": "communication_baseline.txt", "offset_chars": 0}),
                        ("inspect_self", {"area": "network"}),
                    ])
                path = "completion_note.txt" if scenario_id == "committed_progress_prevents_repeated_work" else "assessment.txt"
                actions.extend([
                    ("workspace_write", {"path": path, "mode": "upsert", "content": "Bounded assessment."}),
                    ("report_job_outcome", {"disposition": "completed", "summary": "complete", "report": None,
                                            "readiness": None, "delay_seconds": None}),
                ])
                results = [response("ready")]
                for ordinal, (name, arguments) in enumerate(actions):
                    results.extend([response(None, calls=[call(name, json.dumps(arguments), f"call-{ordinal}")]), response("complete")])
                fake = FakeCompletions(*results)
                args = _arguments([
                    "--backend", "openai-compatible", "--model", CONFIG.model,
                    "--compatible-base-url", CONFIG.base_url, "--compatible-api-key-env", CONFIG.api_key_env,
                    "--compatible-tool-calling", "--compatible-parallel-tool-calls-mode", mode,
                ])
                backend = build_benchmark_backend(args, CONFIG.model, client=client(fake))
                trial = await run_trial(backend, CONFIG.model, 1, scenario_id=scenario_id)
                self.assertTrue(trial.passed, trial.failure_reasons)
                self.assertEqual(trial.final_job_run_status, "completed")
                self.assertEqual([item.name for item in trial.tool_trace], [name for name, _ in actions])
                self.assertTrue(trial.metrics.usage_available)
                self.assertEqual(trial.metrics.provider_requests, 2 * len(actions))
                self.assertEqual(len(trial.provider_calls), 2 * len(actions))
                self.assertIsNone(trial.estimated_cost_usd)
                self.assertEqual(trial.returned_models, ("returned/model-id",))
                self.assertEqual(len(fake.requests), 1 + 2 * len(actions))
                self.assertNotIn("parallel_tool_calls", fake.requests[0])
                for request in fake.requests[1:]:
                    self.assertTrue(request["tools"])
                    if mode == "disable":
                        self.assertIs(request["parallel_tool_calls"], False)
                    else:
                        self.assertNotIn("parallel_tool_calls", request)
                if scenario_id == "confirmed_effect_requires_no_reverification":
                    self.assertEqual(trial.metrics.job_work_episodes, 1)
                    self.assertEqual(trial.metrics.acquisition_tool_calls, 0)
                    self.assertEqual(trial.metrics.effect_tool_calls, 1)

    async def test_benchmark_missing_usage_and_provider_failure_metadata(self):
        fake = FakeCompletions(response("ready"), TimeoutError("private"))
        backend = OpenAICompatibleBackend(config=CONFIG, client=client(fake))
        trial = await run_trial(backend, CONFIG.model, 1)
        self.assertFalse(trial.passed)
        self.assertEqual(trial.failure_category, "timeout")
        self.assertFalse(trial.metrics.usage_available)
        self.assertIsNone(trial.metrics.input_tokens)
        self.assertEqual(trial.provider_calls[0]["status"], "failed")
        self.assertEqual(trial.tool_trace, ())

    async def test_successful_benchmark_without_usage_and_prewarm_exclusion(self):
        for prewarm_usage, work_usage in ((USAGE, None), (None, USAGE)):
            results = [response("ready", usage=prewarm_usage)]
            for name, arguments in (
                ("workspace_write", {"path": "assessment.txt", "mode": "upsert", "content": "bounded"}),
                ("report_job_outcome", {"disposition": "completed", "summary": "complete", "report": None,
                                        "readiness": None, "delay_seconds": None}),
            ):
                results.extend([response(None, calls=[call(name, json.dumps(arguments))], usage=work_usage),
                                response("complete", usage=work_usage)])
            backend = OpenAICompatibleBackend(config=CONFIG, client=client(FakeCompletions(*results)))
            trial = await run_trial(backend, CONFIG.model, 1)
            self.assertTrue(trial.passed, trial.failure_reasons)
            self.assertEqual(trial.metrics.usage_available, work_usage is not None)
            self.assertEqual(trial.metrics.input_tokens, 40 if work_usage is not None else None)
            self.assertEqual(len(trial.provider_calls), 4)
            self.assertIsNone(trial.estimated_cost_usd)

    async def test_continuation_failure_does_not_repeat_workspace_effect_in_harness(self):
        for mode in ("disable", "omit"):
            with self.subTest(mode=mode):
                fake = FakeCompletions(
                    response("ready"), response(None, calls=[call("workspace_write", json.dumps({
                        "path": "assessment.txt", "mode": "upsert", "content": "bounded",
                    }))]), TimeoutError("continuation timed out"),
                )
                backend = OpenAICompatibleBackend(config=replace(CONFIG, parallel_tool_calls_mode=mode),
                                                   client=client(fake))
                trial = await run_trial(backend, CONFIG.model, 1)
                self.assertFalse(trial.passed)
                self.assertEqual(trial.failure_category, "timeout")
                self.assertEqual(len(trial.tool_trace), 1)
                self.assertEqual(trial.tool_trace[0].status, "applied")
                self.assertEqual(trial.metrics.effect_tool_calls, 1)
                self.assertEqual(len(fake.requests), 3)

    async def test_rejected_continuation_accounts_usage_in_runtime_and_benchmark(self):
        prewarm_usage = {"prompt_tokens": 1_000, "completion_tokens": 500, "total_tokens": 1_500,
                         "prompt_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 0}}
        initial_usage = {**USAGE, "prompt_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 0}}
        final_usage = {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27,
                       "prompt_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 0}}
        malformed = response("unusable", usage=final_usage)
        malformed["choices"] = []
        for final, category in (
            (response("partial", finish="length", usage=final_usage), "truncated"),
            (response("refused", refusal="refused", usage=final_usage), "refused"),
            (malformed, "malformed_response"),
        ):
            with self.subTest(category=category):
                fake = FakeCompletions(
                    response("ready", usage=prewarm_usage),
                    response(None, calls=[call("workspace_write", json.dumps({
                        "path": "assessment.txt", "mode": "upsert", "content": "bounded",
                    }))], usage=initial_usage), final,
                )
                backend = OpenAICompatibleBackend(config=CONFIG, client=client(fake))
                trial = await run_trial(backend, CONFIG.model, 1)
                self.assertFalse(trial.passed)
                self.assertEqual(trial.failure_category, category)
                self.assertEqual(trial.metrics.provider_requests, 2)
                self.assertEqual(len(trial.provider_calls), 2)
                self.assertTrue(trial.metrics.usage_available)
                self.assertEqual((trial.metrics.input_tokens, trial.metrics.output_tokens,
                                  trial.metrics.cached_input_tokens), (30, 10, 7))
                self.assertEqual(trial.provider_calls[-1]["status"], "failed")
                self.assertEqual(trial.provider_calls[-1]["usage"]["total_tokens"], 27)
                snapshot = backend.observability.snapshot()
                metrics = snapshot["metrics"]
                self.assertEqual(metrics["provider_requests"], 3)
                self.assertEqual(metrics["provider_failures"], 1)
                self.assertEqual((metrics["input_tokens"], metrics["output_tokens"],
                                  metrics["total_tokens"], metrics["cached_input_tokens"]),
                                 (1_030, 510, 1_540, 207))
                usage = snapshot["provider_usage"][0]
                self.assertEqual(usage["requests"], 3)
                self.assertEqual(usage["total_tokens"], 1_540)
                self.assertEqual(metrics["provider_duration_ms"],
                                 sum(item["duration_ms"] for item in snapshot["provider_calls"]))
                self.assertEqual(usage["duration_ms"], metrics["provider_duration_ms"])
                self.assertEqual(len(fake.requests), 3)
                self.assertEqual(len(trial.tool_trace), 1)
                self.assertEqual(trial.tool_trace[0].status, "applied")
                self.assertEqual(trial.metrics.effect_tool_calls, 1)

    async def test_continuation_timeout_accounts_attempt_and_marks_usage_unknown(self):
        fake = FakeCompletions(
            response("ready"), response(None, calls=[call("workspace_write", json.dumps({
                "path": "assessment.txt", "mode": "upsert", "content": "bounded",
            }))]), TimeoutError("continuation timed out"),
        )
        backend = OpenAICompatibleBackend(config=CONFIG, client=client(fake))
        trial = await run_trial(backend, CONFIG.model, 1)
        self.assertFalse(trial.passed)
        self.assertEqual(trial.failure_category, "timeout")
        self.assertEqual(trial.metrics.provider_requests, 2)
        self.assertEqual(len(trial.provider_calls), 2)
        self.assertFalse(trial.metrics.usage_available)
        self.assertIsNone(trial.metrics.input_tokens)
        self.assertIsNone(trial.metrics.output_tokens)
        snapshot = backend.observability.snapshot()
        self.assertEqual(snapshot["metrics"]["provider_requests"], 3)
        self.assertEqual(snapshot["metrics"]["provider_failures"], 1)
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            self.assertIsNone(snapshot["metrics"][name])
            self.assertIsNone(snapshot["provider_usage"][0][name])
        self.assertFalse(snapshot["provider_usage"][0]["usage_available"])
        self.assertEqual(snapshot["provider_usage"][0]["requests"], 3)
        self.assertIsNone(snapshot["cost"]["estimated_usd"])
        self.assertEqual(snapshot["metrics"]["provider_duration_ms"],
                         sum(item["duration_ms"] for item in snapshot["provider_calls"]))
        self.assertEqual(len(fake.requests), 3)
        self.assertEqual(len(trial.tool_trace), 1)
        self.assertEqual(trial.tool_trace[0].status, "applied")

    async def test_initial_timeout_counts_attempt_with_no_usage_or_effect(self):
        fake = FakeCompletions(response("ready"), TimeoutError("initial timed out"))
        backend = OpenAICompatibleBackend(config=CONFIG, client=client(fake))
        trial = await run_trial(backend, CONFIG.model, 1)
        self.assertFalse(trial.passed)
        self.assertEqual(trial.metrics.provider_requests, 1)
        snapshot = backend.observability.snapshot()
        self.assertEqual(snapshot["metrics"]["provider_requests"], 2)
        self.assertIsNone(snapshot["metrics"]["total_tokens"])
        self.assertFalse(snapshot["provider_usage"][0]["usage_available"])
        self.assertEqual(trial.tool_trace, ())

    async def test_unoffered_tool_is_rejected_by_runtime_executor(self):
        fake = FakeCompletions(response("ready"), response(None, calls=[call("forbidden_tool")]), response("rejected"))
        backend = OpenAICompatibleBackend(config=CONFIG, client=client(fake))
        trial = await run_trial(backend, CONFIG.model, 1)
        self.assertFalse(trial.passed)
        self.assertEqual(len(trial.tool_trace), 1)
        self.assertEqual(trial.tool_trace[0].status, "rejected")
        self.assertEqual(trial.tool_trace[0].name, "forbidden_tool")
