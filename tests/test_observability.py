from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json

from embodied_runtime.observability import (
    PricingCatalog, RunObservability, TokenPrice,
)
from embodied_runtime.pricing import BUILT_IN_PRICING


class Clocks:
    def __init__(self):
        self.wall = datetime(2026, 1, 1, tzinfo=UTC)
        self.mono = 10.0

    def clock(self):
        return self.wall

    def monotonic(self):
        return self.mono


def make(**kwargs):
    clocks = Clocks()
    return RunObservability("R7", clock=clocks.clock,
                            monotonic_clock=clocks.monotonic, **kwargs), clocks


def test_zero_snapshot_and_unknown_cost():
    observed, _ = make()
    snapshot = observed.snapshot()
    assert all(value == 0 for value in snapshot["metrics"].values())
    assert snapshot["cost"] == {
        "status": "unavailable", "estimated_usd": None, "pricing_identity": None,
    }


def test_provider_failure_counts_attempt_and_marks_aggregate_usage_unavailable():
    observed, _ = make()
    observed.provider_completed("provider", "model", "initial", input_tokens=20,
                                cached_input_tokens=7, output_tokens=3,
                                total_tokens=23, duration_ms=14)
    observed.provider_failed("provider", "model", "continuation", duration_ms=2)
    snapshot = observed.snapshot()
    metrics = snapshot["metrics"]
    assert (metrics["provider_requests"], metrics["provider_failures"]) == (2, 1)
    assert metrics["provider_duration_ms"] == 16
    assert (metrics["input_tokens"], metrics["cached_input_tokens"],
            metrics["output_tokens"], metrics["total_tokens"]) == (None, None, None, None)
    assert snapshot["provider_usage"][0]["requests"] == 2
    assert snapshot["provider_usage"][0]["duration_ms"] == 16
    assert not snapshot["provider_usage"][0]["usage_available"]
    assert snapshot["provider_calls"][0]["usage"]["total_tokens"] == 23
    assert snapshot["provider_calls"][1]["usage"] is None
    assert snapshot["cost"]["estimated_usd"] is None


def test_rejected_output_counts_reported_usage_duration_and_cost_once():
    pricing = PricingCatalog({("p", "m"): TokenPrice(
        Decimal("1"), Decimal("0.25"), Decimal("2"), Decimal("1.5"))}, "v1")
    observed, _ = make(pricing=pricing)
    observed.provider_completed("p", "m", "initial", duration_ms=14)
    observed.provider_failed("p", "m", "continuation", duration_ms=27,
                             failure_category="truncated", usage={
                                 "input_tokens": 1_000_000, "cached_input_tokens": 200_000,
                                 "cache_write_tokens": 100_000, "output_tokens": 500_000,
                                 "total_tokens": 1_500_000,
                             })
    snapshot = observed.snapshot()
    metrics = snapshot["metrics"]
    assert metrics["provider_requests"] == 2
    assert metrics["provider_failures"] == 1
    assert metrics["provider_duration_ms"] == 41
    assert metrics["input_tokens"] == 1_000_000
    assert metrics["total_tokens"] == 1_500_000
    assert snapshot["provider_usage"][0]["requests"] == 2
    assert snapshot["provider_usage"][0]["duration_ms"] == 41
    assert snapshot["provider_usage"][0]["usage_available"]
    assert snapshot["cost"]["estimated_usd"] == "1.900000"
    assert snapshot["dimensions"]["provider_models"]["p/m"] == 2
    assert snapshot["dimensions"]["provider_stages"] == {"initial": 1, "continuation": 1}
    calls = snapshot["provider_calls"]
    assert len(calls) == 2
    assert calls[1]["status"] == "failed"
    assert calls[1]["failure_category"] == "truncated"
    assert calls[1]["usage_available"]
    assert [event.status for event in observed.events(component="cognition")] == ["completed", "failed"]


def test_timeout_without_response_never_becomes_zero_usage_or_estimated_cost():
    observed, _ = make(pricing=BUILT_IN_PRICING)
    observed.provider_failed("openai-responses", "gpt-6-luna", "initial", duration_ms=50,
                             failure_category="timeout")
    snapshot = observed.snapshot()
    assert snapshot["metrics"]["provider_requests"] == 1
    assert snapshot["metrics"]["provider_failures"] == 1
    assert snapshot["metrics"]["provider_duration_ms"] == 50
    assert snapshot["metrics"]["total_tokens"] is None
    assert snapshot["provider_usage"][0]["requests"] == 1
    assert snapshot["provider_usage"][0]["total_tokens"] is None
    assert not snapshot["provider_usage"][0]["usage_available"]
    assert snapshot["cost"]["status"] == "unavailable"
    observed.provider_completed("openai-responses", "gpt-6-luna", "continuation", input_tokens=5)
    later = observed.snapshot()
    assert later["metrics"]["provider_requests"] == 2
    assert later["metrics"]["total_tokens"] is None
    assert later["cost"]["status"] == "unavailable"


def test_rejected_partial_or_invalid_usage_remains_visible_and_unavailable():
    for usage, expected_input in (
        ({"input_tokens": 7, "output_tokens": 2}, 7),
        ({"input_tokens": True, "output_tokens": 2, "total_tokens": 3}, None),
        ({"input_tokens": -1, "output_tokens": 2, "total_tokens": 1}, None),
    ):
        observed, _ = make()
        observed.provider_failed("p", "m", "initial", usage=usage,
                                 failure_category="malformed_response")
        snapshot = observed.snapshot()
        assert snapshot["metrics"]["provider_requests"] == 1
        assert snapshot["metrics"]["total_tokens"] is None
        assert snapshot["provider_calls"][0]["usage"]["input_tokens"] == expected_input
        assert snapshot["provider_calls"][0]["usage"]["output_tokens"] == 2
        assert not snapshot["provider_calls"][0]["usage_available"]


def test_configured_cost_and_missing_price_are_explicit():
    pricing = PricingCatalog({("p", "m"): TokenPrice(
        Decimal("1"), Decimal("0.5"), Decimal("2"))}, "test-v1")
    observed, _ = make(pricing=pricing)
    observed.provider_completed("p", "m", "initial", input_tokens=1_000_000,
                                cached_input_tokens=1_000_000,
                                output_tokens=1_000_000)
    # Cached reads are a subset of input, not an additional million input tokens.
    assert observed.snapshot()["cost"]["estimated_usd"] == "2.500000"
    unknown, _ = make(pricing=pricing)
    unknown.provider_completed("other", "m", "initial", input_tokens=1)
    assert unknown.snapshot()["cost"]["status"] == "unavailable"


def test_cost_uses_mutually_exclusive_input_categories_and_rejects_bad_usage():
    pricing = PricingCatalog({("p", "m"): TokenPrice(
        Decimal("1"), Decimal("0.25"), Decimal("2"), Decimal("1.5"))}, "v1")
    observed, _ = make(pricing=pricing)
    observed.provider_completed("p", "m", "initial", input_tokens=1_000_000,
                                cached_input_tokens=200_000,
                                cache_write_tokens=100_000)
    # 700k uncached + 200k cached + 100k writes = .7 + .05 + .15.
    assert observed.snapshot()["cost"]["estimated_usd"] == "0.900000"
    inconsistent, _ = make(pricing=pricing)
    inconsistent.provider_completed("p", "m", "initial", input_tokens=5,
                                    cached_input_tokens=6)
    assert inconsistent.snapshot()["cost"]["status"] == "unavailable"
    no_write_rate = PricingCatalog({("p", "m"): TokenPrice(
        Decimal("1"), Decimal("0.25"), Decimal("2"))}, "v2")
    unpriced_write, _ = make(pricing=no_write_rate)
    unpriced_write.provider_completed("p", "m", "initial", input_tokens=5,
                                      cache_write_tokens=1)
    assert unpriced_write.snapshot()["cost"]["status"] == "unavailable"


def test_built_in_pricing_has_expected_openai_responses_rates():
    assert BUILT_IN_PRICING.identity == "public-built-in-pricing-2026-10-05"
    expected = {
        "gpt-5.6-luna": ("0.20", "0.02", "0.25", "1.20"),
        "gpt-5.6-sol": ("4.00", "0.40", "5.00", "20.00"),
        "gpt-6.1-sol": ("2.00", "0.10", "2.50", "10.00"),
        "gpt-6-luna": ("0.10", "0.01", "0.125", "0.50"),
        "gpt-6-astra": ("10.00", "1.00", "12.50", "50.00"),
    }
    assert set(BUILT_IN_PRICING.rates) == {
        ("openai-responses", model) for model in expected
    }
    for model, values in expected.items():
        rate = BUILT_IN_PRICING.rates[("openai-responses", model)]
        assert (
            rate.input_per_million,
            rate.cached_input_per_million,
            rate.cache_write_per_million,
            rate.output_per_million,
        ) == tuple(Decimal(value) for value in values)
        assert rate.max_input_tokens == 272_000


def test_built_in_pricing_calculates_real_provider_usage_without_double_counting():
    observed, _ = make(pricing=BUILT_IN_PRICING)
    # Four individually supported requests aggregate to the representative 1M input run.
    for _ in range(4):
        observed.provider_completed(
            "openai-responses", "gpt-5.6-luna", "initial",
            input_tokens=250_000, cached_input_tokens=50_000,
            cache_write_tokens=25_000, output_tokens=125_000,
        )
    cost = observed.snapshot()["cost"]
    assert cost["estimated_usd"] == "0.769000"
    assert cost["components"]["cognition"]["estimated_usd"] == "0.769000"
    assert cost["components"]["tts"]["estimated_usd"] == "0.000000"


def test_built_in_pricing_fails_closed_above_per_request_context_limit():
    for model in ("gpt-5.6-sol", "gpt-6.1-sol", "gpt-6-luna", "gpt-6-astra"):
        at_limit, _ = make(pricing=BUILT_IN_PRICING)
        at_limit.provider_completed(
            "openai-responses", model, "initial", input_tokens=272_000,
        )
        assert at_limit.snapshot()["cost"]["status"] == "estimated"

        above_limit, _ = make(pricing=BUILT_IN_PRICING)
        above_limit.provider_completed(
            "openai-responses", model, "initial", input_tokens=272_001,
        )
        assert above_limit.snapshot()["cost"]["status"] == "unavailable"
        assert above_limit.snapshot()["cost"]["estimated_usd"] is None

    mixed, _ = make(pricing=BUILT_IN_PRICING)
    mixed.provider_completed(
        "openai-responses", "gpt-5.6-luna", "initial", input_tokens=1,
    )
    mixed.provider_completed(
        "openai-responses", "gpt-5.6-luna", "continuation", input_tokens=272_001,
    )
    assert mixed.snapshot()["cost"]["status"] == "unavailable"


def test_built_in_pricing_does_not_fall_back_for_model_override():
    observed, _ = make(pricing=BUILT_IN_PRICING)
    observed.provider_completed(
        "openai-responses", "gpt-5.6-sol-alias", "initial", input_tokens=1,
    )
    cost = observed.snapshot()["cost"]
    assert cost["status"] == "unavailable"
    assert cost["estimated_usd"] is None
    assert cost["pricing_identity"] == BUILT_IN_PRICING.identity


def test_snapshot_is_detached_and_local_activity_has_no_cost():
    observed, _ = make()
    first = observed.snapshot()
    first["metrics"]["tts_generations"] = 99
    assert observed.snapshot()["metrics"]["tts_generations"] == 0
    observed.increment("tts_generations", dimension=("tts_providers", "local/piper"))
    assert observed.snapshot()["cost"]["estimated_usd"] is None


def test_events_are_bounded_filtered_and_strings_are_bounded():
    observed, _ = make(event_capacity=2)
    observed.event("camera", "capture", "completed", metadata={"long": "x" * 1000})
    observed.event("jobs", "work", "completed",
                   identifiers={"job_run_id": "R" * 1000})
    events = observed.events()
    assert len(events) == 2
    assert events[0].component == "camera"  # initial runtime event was evicted
    assert len(events[0].metadata[0][1]) == 160
    assert observed.events(component="jobs") == (events[1],)
    assert len(events[1].identifiers[0][1]) == 160
    assert observed.events(since_monotonic=11) == ()


def test_camera_vision_and_job_work_counters_are_independent():
    observed, _ = make()
    observed.increment("camera_captures")
    observed.increment("manual_job_work")
    metrics = observed.snapshot()["metrics"]
    assert metrics["vision_acquisitions"] == 0
    assert metrics["automatic_job_work"] == 0


def test_finalize_once_freezes_late_updates_and_writes_safe_summary(tmp_path):
    observed, clocks = make()
    observed.provider_completed("p", "m", "initial", input_tokens=2)
    clocks.wall += timedelta(seconds=5)
    clocks.mono += 5
    first = observed.finalize("interrupted", shutdown="interrupted", directory=tmp_path)
    observed.increment("runtime_errors")
    clocks.mono += 5
    second = observed.finalize("failed", directory=tmp_path)
    assert first == second
    assert first["run"]["elapsed_seconds"] == 5
    assert first["run"]["status"] == "interrupted"
    persisted = json.loads((tmp_path / "summary.json").read_text())
    serialized = json.dumps(persisted)
    assert persisted["schema_version"] == 5
    assert persisted["provider_usage"] == [{
        "provider": "p", "model": "m", "requests": 1,
        "usage_available": True,
        "input_tokens": 2, "cached_input_tokens": 0,
        "cache_write_tokens": 0, "output_tokens": 0,
        "total_tokens": 2, "duration_ms": 0,
    }]
    assert observed.summary_persistence == ("written", None)
    assert "prompt" not in serialized and "transcript" not in serialized


def test_summary_write_failure_is_reported_not_claimed(tmp_path):
    observed, _ = make()
    missing = tmp_path / "missing"
    observed.finalize("completed", directory=missing)
    status, error = observed.summary_persistence
    assert status == "failed"
    assert error == "FileNotFoundError"
    assert not (missing / "summary.json").exists()


def test_banner_contains_stable_sections():
    observed, _ = make()
    snapshot = observed.finalize("completed", shutdown="normal")
    banner = observed.banner(snapshot)
    assert "R7 SUMMARY" in banner
    assert "provider_requests:    0" in banner
    assert "estimated_cost_usd:   unavailable" in banner
    assert "tts_characters:       0" in banner
    assert "camera_captures:      0" in banner


def test_elevenlabs_character_usage_combines_with_cognition_without_float_drift():
    observed, _ = make(pricing=BUILT_IN_PRICING)
    observed.provider_completed("openai-responses", "gpt-5.6-luna", "initial",
                                input_tokens=5, output_tokens=5)
    observed.tts_synthesized("elevenlabs", "eleven_flash_v2_5",
                             characters=31, duration_ms=12)
    observed.tts_synthesized("elevenlabs", "eleven_flash_v2_5",
                             characters=46, duration_ms=8)
    snapshot = observed.snapshot()
    assert snapshot["tts_usage"] == [{
        "provider": "elevenlabs", "model": "eleven_flash_v2_5",
        "usage_basis": "text_length_estimate",
        "syntheses": 2, "characters": 77, "duration_ms": 20,
    }]
    assert snapshot["cost"]["components"]["tts"]["estimated_usd"] == "0.003850"
    assert snapshot["cost"]["estimated_usd"] == "0.003857"


def test_unknown_used_tts_model_fails_cost_closed_and_finalize_is_frozen():
    observed, _ = make(pricing=BUILT_IN_PRICING)
    observed.tts_synthesized("elevenlabs", "unknown", characters=1)
    first = observed.finalize("completed")
    assert first["cost"]["status"] == "unavailable"
    observed.tts_synthesized("elevenlabs", "eleven_flash_v2_5", characters=1000)
    assert observed.finalize("failed") == first
