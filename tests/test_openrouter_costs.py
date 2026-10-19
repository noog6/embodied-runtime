"""Offline response accounting, including real SDK parsing without network calls."""

import asyncio
from dataclasses import replace
from decimal import Decimal, localcontext
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from embodied_runtime.benchmarks import run_trial
from embodied_runtime.benchmarks.models import BenchmarkReport
from embodied_runtime.benchmarks.runner import _scenario_cost, render_report
from embodied_runtime.cognition import CognitionError, CognitionToolResult
from embodied_runtime.cognition.openai_compatible import OpenAICompatibleBackend
from embodied_runtime.config import VisionBackendConfig
from embodied_runtime.monetary import openrouter_cost, parse_usd
from embodied_runtime.observability import PricingCatalog, RunObservability, TokenPrice
from embodied_runtime.perception import (
    OpenAICompatibleVisualPerceptionBackend, OrderedVisualPerceptionBackend,
)
from embodied_runtime.pricing import BUILT_IN_PRICING
from tests.test_openai_compatible import CONFIG, TOOL, FakeCompletions, call, client, response
from tests.test_portable_visual_perception import FRAME, FOCUS


FULL_USAGE = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
              "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}
TOKENS = {"input_tokens": 10, "cached_input_tokens": 0, "cache_write_tokens": 0,
          "output_tokens": 3, "total_tokens": 13}


def charged(value, *, complete=True, **kwargs):
    usage = {**FULL_USAGE, "cost": value} if complete else {"cost": value}
    # This other monetary measure must never be added, substituted, or persisted.
    usage["cost_details"] = {"upstream_inference_cost": 999, "private": "PRIVATE"}
    return response(usage=usage, **kwargs)


def backend(*results, observed=None, config=CONFIG):
    fake = FakeCompletions(*results)
    observed = observed if observed is not None else RunObservability(pricing=BUILT_IN_PRICING)
    return OpenAICompatibleBackend(config=config, client=client(fake), observability=observed), observed, fake


class MoneyValidationTests(unittest.TestCase):
    def test_valid_values_and_bounded_invalid_values(self):
        for value in (0, 0.0, "0", "-0.0", -0.0, Decimal("0"), ".125", "1e-12", 0.1, Decimal("0.25")):
            with self.subTest(value=value):
                self.assertIsNotNone(parse_usd(value))
        for value in (None, True, False, -1, "-0.1", Decimal("-1"), float("nan"),
                      float("inf"), Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"),
                      "NaN", "inf", "", "1 USD", " 1 ", {}, [], object(),
                      "9" * 129, "1e999999999999999999999", "1e-9999999",
                      Decimal("1e999999"), "0." + "1" * 50, 10**10000,
                      "1e13", "1e-25"):
            with self.subTest(type=type(value), short=repr(value)[:90] if not isinstance(value, int) else None):
                self.assertIsNone(parse_usd(value))

    def test_only_verified_openrouter_endpoint_has_cost_semantics(self):
        result = charged("0.25")
        self.assertEqual(openrouter_cost(result, base_url=CONFIG.base_url), Decimal(".25"))
        for url in ("https://other.example/v1", "https://openrouter.ai.evil.example/api/v1",
                    "http://openrouter.ai/api/v1", "https://openrouter.ai:8443/api/v1",
                    "https://openrouter.ai/unverified", "https://user@openrouter.ai/api/v1"):
            self.assertIsNone(openrouter_cost(result, base_url=url))
        extension_only = SimpleNamespace(usage=SimpleNamespace(model_extra={"cost": ".3"}))
        self.assertEqual(openrouter_cost(extension_only, base_url=CONFIG.base_url), Decimal(".3"))


class OpenRouterResponseCostTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_model_multiple_models_and_no_catalog_required(self):
        observed = RunObservability()
        for model, amount in (("new/never-catalogued", "0.1"), ("other/model", "0.2"),
                              ("new/never-catalogued", "0.003")):
            adapter, _, _ = backend(charged(amount), observed=observed,
                                    config=replace(CONFIG, model=model))
            self.assertEqual(await adapter.respond("hello"), "answer")
        snapshot = observed.snapshot()
        cost = snapshot["cost"]
        self.assertEqual(cost["status"], "reported")
        self.assertEqual(cost["total_usd"], "0.303000")
        self.assertEqual(cost["reported_calls"], 3)
        self.assertIsNone(cost["pricing_identity"])
        self.assertIsNone(cost["estimated_usd"])
        self.assertEqual(snapshot["metrics"]["provider_requests"], 3)
        self.assertEqual(snapshot["metrics"]["total_tokens"], 39)
        self.assertEqual(snapshot["provider_calls"][0]["requested_model"], "new/never-catalogued")
        self.assertEqual(snapshot["provider_calls"][0]["returned_model"], "returned/model-id")

    async def test_zero_with_incomplete_tokens_is_known_money(self):
        adapter, observed, _ = backend(charged(0, complete=False))
        await adapter.respond("hello")
        snapshot = observed.snapshot()
        self.assertEqual(snapshot["cost"]["total_usd"], "0.000000")
        self.assertEqual(snapshot["cost"]["status"], "reported")
        self.assertIsNone(snapshot["metrics"]["total_tokens"])
        self.assertFalse(snapshot["provider_calls"][0]["usage_available"])
        self.assertIsNone(snapshot["provider_calls"][0]["usage"]["input_tokens"])

    async def test_invalid_cost_does_not_fail_response_or_assume_free(self):
        for value in (None, True, False, -1, "wrong", float("nan"), float("inf"), "1e999999"):
            with self.subTest(value=value):
                adapter, observed, _ = backend(charged(value))
                self.assertEqual(await adapter.respond("hello"), "answer")
                cost = observed.snapshot()["cost"]
                self.assertEqual(cost["status"], "unavailable")
                self.assertIsNone(cost["total_usd"])
                self.assertEqual(cost["unknown_calls"], 1)
        adapter, observed, _ = backend(response(usage={**FULL_USAGE,
            "cost_details": {"upstream_inference_cost": 42}}))
        await adapter.respond("hello")
        self.assertEqual(observed.snapshot()["cost"]["unknown_calls"], 1)

    async def test_rejected_responses_retain_cost_once(self):
        results = [charged(".12", finish="length"), charged(".12", refusal="private refusal"),
                   charged(".12", content=""), charged(".12", calls=[call(), call()])]
        malformed = charged(".12")
        malformed["choices"] = []
        results.append(malformed)
        for result in results:
            with self.subTest(result=result["choices"]):
                adapter, observed, _ = backend(result)
                with self.assertRaises(CognitionError):
                    await adapter.respond("hello", tools=[TOOL],
                        tool_executor=lambda _: None, refreshed_instructions=lambda: "fresh")
                snapshot = observed.snapshot()
                self.assertEqual(snapshot["cost"]["total_usd"], "0.120000")
                self.assertEqual(snapshot["cost"]["reported_calls"], 1)
                self.assertEqual(snapshot["metrics"]["provider_failures"], 1)

    async def test_tool_continuation_and_failure_unknown_cost(self):
        adapter, observed, fake = backend(charged(".1", content=None, calls=[call()]),
                                          charged(".2"))
        async def execute(_):
            return CognitionToolResult("done")
        await adapter.respond("hello", tools=[TOOL], tool_executor=execute,
                              refreshed_instructions=lambda: "fresh")
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(observed.snapshot()["cost"]["total_usd"], "0.300000")
        adapter, observed, _ = backend(charged(".1"), TimeoutError("private"), charged(".2"))
        await adapter.respond("hello")
        with self.assertRaises(CognitionError):
            await adapter.respond("hello")
        await adapter.respond("hello")
        cost = observed.snapshot()["cost"]
        self.assertEqual(cost["status"], "unavailable")
        self.assertIsNone(cost["total_usd"])
        self.assertEqual(cost["known_usd"], "0.300000")
        self.assertEqual(cost["unknown_calls"], 1)

    async def test_cancelled_attempt_without_response_is_unknown_and_propagates(self):
        async def cancel():
            raise asyncio.CancelledError()
        adapter, observed, _ = backend(cancel)
        with self.assertRaises(asyncio.CancelledError):
            await adapter.respond("hello")
        snapshot = observed.snapshot()
        self.assertEqual(snapshot["provider_calls"][0]["status"], "cancelled")
        self.assertEqual(snapshot["cost"]["unknown_calls"], 1)

    async def test_non_router_with_router_model_name_ignores_cost(self):
        adapter, observed, _ = backend(charged(".1"), config=replace(
            CONFIG, model="openai/gpt-6-luna", base_url="https://other.example/v1"))
        await adapter.respond("hello")
        self.assertEqual(observed.snapshot()["cost"]["status"], "unavailable")
        self.assertIsNone(observed.snapshot()["provider_calls"][0]["reported_cost_usd"])

    async def test_actual_sdk_response_representation_preserves_extension(self):
        try:
            from openai.types import CompletionUsage
            from openai.types.chat import ChatCompletion
            from openai.types.chat.chat_completion import Choice
        except ImportError:
            self.skipTest("optional OpenAI SDK is not installed")
        payload = charged(0.000000123, complete=False)
        payload.update(id="offline", object="chat.completion", created=0)
        # Validate a complete SDK response, then exercise the SDK's permissive
        # construction of provider payloads whose required token fields are absent.
        validated = ChatCompletion.model_validate({**payload, "usage": {**FULL_USAGE, **payload["usage"]}})
        self.assertEqual(openrouter_cost(validated, base_url=CONFIG.base_url), Decimal("0.000000123"))
        parsed = ChatCompletion.model_construct(**{
            **payload, "usage": CompletionUsage.model_construct(**payload["usage"]),
            "choices": [Choice.model_validate(payload["choices"][0])],
        })
        self.assertEqual(parsed.usage.model_extra["cost"], 0.000000123)
        adapter, observed, fake = backend(parsed)
        await adapter.respond("hello")
        self.assertEqual(observed.snapshot()["cost"]["total_usd"], "0.000000123")
        self.assertFalse(observed.snapshot()["provider_calls"][0]["usage_available"])
        self.assertEqual(len(fake.requests), 1)
        self.assertNotIn("usage", fake.requests[0])
        self.assertNotIn("stream_options", fake.requests[0])

    async def test_vision_cost_and_rejected_fallback_cost_remain_separate(self):
        observed = RunObservability(pricing=BUILT_IN_PRICING)
        config = VisionBackendConfig("openai-compatible", "new/vision", "TEST_KEY", CONFIG.base_url)
        adapters = [OpenAICompatibleVisualPerceptionBackend(config=config, client=client(FakeCompletions(result)))
                    for result in (charged(".02", finish="length"), charged(".03", complete=False))]
        chain = OrderedVisualPerceptionBackend(adapters, observability=observed)
        await chain.interpret(FRAME, FOCUS)
        snapshot = observed.snapshot()
        cost = snapshot["cost"]
        self.assertEqual(cost["components"]["vision"]["total_usd"], "0.050000")
        self.assertEqual(cost["components"]["cognition"]["total_usd"], "0.000000")
        self.assertIsNone(snapshot["metrics"]["vision_total_tokens"])
        self.assertEqual(snapshot["metrics"]["provider_requests"], 0)
        self.assertEqual(snapshot["metrics"]["vision_provider_requests"], 2)
        self.assertEqual(snapshot["provider_calls"][0]["status"], "failed")
        self.assertEqual(snapshot["provider_calls"][1]["attempt_order"], 2)
        self.assertTrue(snapshot["provider_calls"][1]["fallback"])

    async def test_non_openrouter_vision_ignores_reported_cost(self):
        observed = RunObservability()
        config = VisionBackendConfig("openai-compatible", "openai/router-like-name", "TEST_KEY",
                                     "https://other.example/v1")
        adapter = OpenAICompatibleVisualPerceptionBackend(config=config,
            client=client(FakeCompletions(charged(".02"))), observability=observed)
        await adapter.interpret(FRAME, FOCUS)
        snapshot = observed.snapshot()
        self.assertEqual(snapshot["cost"]["unknown_calls"], 1)
        self.assertIsNone(snapshot["provider_calls"][0]["reported_cost_usd"])


class MonetaryAggregationTests(unittest.TestCase):
    def test_prefer_reported_without_double_counting_and_fallback_per_attempt(self):
        pricing = PricingCatalog({("openai-compatible", CONFIG.model): TokenPrice(
            Decimal(1), Decimal(1), Decimal(1))}, "offline")
        observed = RunObservability(pricing=pricing)
        observed.provider_call("openai-compatible", CONFIG.model, "initial", usage=TOKENS,
                               reported_cost_usd=Decimal(".1"))
        observed.provider_call("openai-compatible", CONFIG.model, "initial", usage=TOKENS)
        cost = observed.snapshot()["cost"]
        self.assertEqual(cost["total_usd"], "0.100013")
        self.assertEqual(cost["reported_usd"], "0.100000")
        self.assertEqual(cost["catalog_estimated_usd"], "0.000013")
        self.assertEqual(cost["status"], "mixed")
        self.assertIsNone(cost["estimated_usd"])

    def test_mixed_router_direct_and_tts_and_invalid_catalog_rates(self):
        observed = RunObservability(pricing=BUILT_IN_PRICING)
        observed.provider_call("openai-compatible", "new/model", "initial", reported_cost_usd=Decimal(".1"))
        observed.provider_completed("openai-responses", "gpt-5.6-luna", "initial",
                                    input_tokens=5, output_tokens=5)
        observed.tts_synthesized("elevenlabs", "eleven_flash_v2_5", characters=77)
        cost = observed.snapshot()["cost"]
        self.assertEqual(cost["status"], "mixed")
        self.assertEqual(cost["total_usd"], "0.103857")
        self.assertEqual(cost["catalog_estimated_usd"], "0.003857")
        self.assertEqual(cost["reported_calls"], 1)
        self.assertEqual(cost["estimated_calls"], 2)
        self.assertEqual(cost["components"]["tts"]["status"], "estimated")
        for invalid in (Decimal("NaN"), Decimal("Infinity"), Decimal(-1)):
            pricing = PricingCatalog({("p", "m"): TokenPrice(invalid, Decimal(0), Decimal(1))}, "bad")
            invalid_catalog = RunObservability(pricing=pricing)
            invalid_catalog.provider_completed("p", "m", "initial", input_tokens=1)
            self.assertIsNone(invalid_catalog.snapshot()["cost"]["total_usd"])

    def test_lifetime_totals_outlive_diagnostics_and_decimal_context(self):
        observed = RunObservability(event_capacity=2)
        with localcontext() as context:
            context.prec = 6
            for _ in range(301):
                observed.provider_call("openai-compatible", "new/model", "initial",
                                       reported_cost_usd=Decimal("0.123456789123456789"))
            snapshot = observed.snapshot()
        self.assertEqual(len(snapshot["provider_calls"]), 2)
        self.assertEqual(len(observed.events()), 2)
        self.assertEqual(snapshot["cost"]["total_usd"], "37.160493526160493489")
        self.assertEqual(snapshot["cost"]["reported_calls"], 301)

    def test_summary_serialization_basis_privacy_and_historical_banner(self):
        observed = RunObservability("R7")
        observed.provider_call("openai-compatible", "new/model", "initial", reported_cost_usd=Decimal(".1"))
        with tempfile.TemporaryDirectory() as directory:
            observed.finalize("completed", directory=Path(directory))
            summary = json.loads((Path(directory) / "summary.json").read_text())
        self.assertEqual(summary["schema_version"], 6)
        self.assertEqual(summary["cost"]["currency"], "USD")
        self.assertEqual(summary["provider_calls"][0]["cost_basis"], "reported")
        self.assertEqual(summary["provider_calls"][0]["reported_cost_usd"], "0.100000")
        banner = observed.banner(summary)
        self.assertIn("total_cost_usd:       0.100000", banner)
        self.assertIn("cost_basis:           reported", banner)
        # Historical summaries retain their original estimated field semantics.
        for version in (3, 4, 5):
            summary["schema_version"] = version
            summary["cost"] = {"status": "estimated", "estimated_usd": "0.200000", "pricing_identity": "old"}
            if version < 5:
                summary["metrics"] = {key: value for key, value in summary["metrics"].items()
                                      if not key.startswith("vision_")}
            self.assertIn("estimated_cost_usd:   0.200000", observed.banner(summary))
            self.assertIn("vision_requests:      unavailable", observed.banner(summary))

    def test_benchmark_delta_ignores_prewarm_unknown_and_keeps_unknown_in_scenario(self):
        observed = RunObservability()
        observed.provider_call("openai-compatible", "new/model", "prewarm")
        before = observed.snapshot()
        observed.provider_call("openai-compatible", "new/model", "initial", reported_cost_usd=Decimal(".1"))
        known = _scenario_cost(before, observed.snapshot())
        self.assertEqual(known["cost_usd"], "0.100000")
        self.assertEqual(known["cost_basis"], "reported")
        observed.provider_call("openai-compatible", "new/model", "continuation", status="failed")
        unknown = _scenario_cost(before, observed.snapshot())
        self.assertIsNone(unknown["cost_usd"])
        self.assertEqual(unknown["reported_cost_usd"], "0.100000")
        self.assertEqual(unknown["unknown_cost_calls"], 1)


class BenchmarkReportedCostTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_harness_serializes_and_renders_reported_trial_cost(self):
        results = [response("ready", usage=None)]  # prewarm has no monetary evidence
        for name, arguments in (
            ("workspace_write", {"path": "assessment.txt", "mode": "upsert", "content": "bounded"}),
            ("report_job_outcome", {"disposition": "completed", "summary": "complete", "report": None,
                                    "readiness": None, "delay_seconds": None}),
        ):
            results.extend([charged(".01", content=None, calls=[call(name, json.dumps(arguments))]),
                            charged(".02")])
        adapter, _, _ = backend(*results)
        trial = await run_trial(adapter, CONFIG.model, 1,
                               scenario_id="confirmed_effect_requires_no_reverification")
        self.assertTrue(trial.passed, trial.failure_reasons)
        self.assertEqual(trial.metrics.provider_requests, 4)
        self.assertEqual(trial.cost_usd, "0.060000")
        self.assertEqual(trial.cost_basis, "reported")
        self.assertIsNone(trial.estimated_cost_usd)
        report = BenchmarkReport("2026-10-19T12:00:00+00:00", trial.scenario_id, (trial,), BUILT_IN_PRICING.identity)
        serialized = report.to_json()
        self.assertEqual(json.loads(serialized)["format_version"], 4)
        self.assertNotIn("upstream_inference_cost", serialized)
        self.assertNotIn("PRIVATE", serialized)
        rendered = render_report(report)
        self.assertIn("report cost (reported): $0.060000", rendered)
        self.assertNotIn("estimated report cost", rendered)
        estimated = replace(trial, cost_usd="0.030000", cost_basis="estimated",
                            estimated_cost_usd="0.030000", reported_cost_usd="0.000000",
                            catalog_estimated_cost_usd="0.030000")
        mixed_report = replace(report, trials=(trial, estimated))
        self.assertIn("report cost (mixed): $0.090000", render_report(mixed_report))
        unknown = replace(trial, cost_usd=None, cost_basis="unavailable", unknown_cost_calls=1)
        self.assertIn("report cost (unavailable): unavailable",
                      render_report(replace(report, trials=(trial, estimated, unknown))))
