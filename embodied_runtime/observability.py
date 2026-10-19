"""Bounded, runtime-owned accounting for one process run.

This module deliberately stores operational metadata only.  Callers must not put
prompts, transcripts, media, credentials, or provider response bodies in events.
"""

from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
import json
from pathlib import Path
from threading import Lock
from time import monotonic
from types import MappingProxyType
from typing import Any

from embodied_runtime.monetary import add_usd, parse_usd, usd_string


MAX_EVENT_STRING = 160
MAX_EVENT_METADATA_ITEMS = 12
MAX_EVENT_IDENTIFIERS = 8
DEFAULT_EVENT_CAPACITY = 200
TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "total_tokens",
)


@dataclass(frozen=True, slots=True)
class TokenPrice:
    """USD per million tokens; cached input is priced independently."""

    input_per_million: Decimal
    cached_input_per_million: Decimal
    output_per_million: Decimal
    cache_write_per_million: Decimal | None = None
    max_input_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class CharacterPrice:
    """Public USD rate for one thousand successfully synthesized characters."""

    usd_per_thousand: Decimal


@dataclass(frozen=True, slots=True)
class PricingCatalog:
    rates: Mapping[tuple[str, str], TokenPrice]
    identity: str
    character_rates: Mapping[tuple[str, str], CharacterPrice] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    timestamp: str
    monotonic_seconds: float
    component: str
    operation: str
    status: str
    severity: str = "info"
    source: str | None = None
    duration_ms: int | None = None
    error: str | None = None
    identifiers: tuple[tuple[str, str | int], ...] = ()
    metadata: tuple[tuple[str, str | int | float | bool | None], ...] = ()


_COUNTERS = (
    "provider_requests", "provider_failures", "input_tokens",
    "cached_input_tokens", "cache_write_tokens", "output_tokens", "total_tokens",
    "provider_duration_ms", "wake_capture_attempts", "wake_captures",
    "stt_captures", "voice_turns", "tts_generations", "tts_characters",
    "voice_failures", "tts_primary_failures", "tts_fallbacks",
    "tts_fallback_failures", "camera_captures", "camera_failures",
    "vision_acquisitions", "vision_failures", "attention_episodes_started",
    "attention_episodes_completed", "operator_attention_episodes",
    "automatic_attention_episodes", "job_runs_started", "job_runs_completed",
    "job_runs_failed", "job_runs_stopped", "manual_job_work",
    "automatic_job_work", "continuations_accepted", "runtime_errors",
    "interruptions", "resource_failures",
    "vision_provider_requests", "vision_provider_failures", "vision_provider_duration_ms",
    "vision_input_tokens", "vision_cached_input_tokens", "vision_cache_write_tokens",
    "vision_output_tokens", "vision_total_tokens", "vision_fallbacks",
)


class RunObservability:
    """Thread-safe, best-effort, session-local metrics and bounded events."""

    def __init__(self, run_id: str | None = None, *, event_capacity: int = DEFAULT_EVENT_CAPACITY,
                 pricing: PricingCatalog | None = None, clock=None, monotonic_clock=None) -> None:
        if event_capacity < 1:
            raise ValueError("event_capacity must be positive")
        self.run_id = run_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_clock or monotonic
        self._started_at = self._clock()
        self._started_monotonic = self._monotonic()
        self._pricing = pricing
        self._counters = Counter({name: 0 for name in _COUNTERS})
        self._dimensions: dict[str, Counter[str]] = {
            "provider_models": Counter(), "provider_stages": Counter(),
            "vision_provider_models": Counter(), "vision_provider_stages": Counter(),
            "tts_providers": Counter(), "attention_sources": Counter(),
            "attention_completion_reasons": Counter(), "job_work_outcomes": Counter(),
        }
        self._provider_usage: dict[tuple[str, str, str], Counter[str]] = {}
        self._tts_usage: dict[tuple[str, str, str], Counter[str]] = {}
        # Lifetime monetary accumulators are independent of tokens and the deque.
        self._money = {role: {"reported": Decimal(0), "estimated": Decimal(0),
                              "reported_calls": 0, "estimated_calls": 0, "unknown_calls": 0}
                       for role in ("cognition", "vision", "tts")}
        self._missing_usage: set[tuple[str, str, str]] = set()
        self._missing_token_fields: dict[tuple[str, str, str], set[str]] = {}
        self._provider_calls: deque[dict[str, object]] = deque(maxlen=event_capacity)
        self._events: deque[RuntimeEvent] = deque(maxlen=event_capacity)
        self._lock = Lock()
        self._final: dict[str, Any] | None = None
        self._summary_persistence = "not_requested"
        self._summary_persistence_error: str | None = None
        self.event("runtime", "run", "started")

    @staticmethod
    def _short(value: object) -> str:
        return str(value)[:MAX_EVENT_STRING]

    def event(self, component: str, operation: str, status: str, *, severity: str = "info",
              source: str | None = None, duration_ms: int | None = None,
              error: str | None = None,
              identifiers: Mapping[str, str | int] | None = None,
              metadata: Mapping[str, object] | None = None) -> None:
        """Append sanitized metadata; observability never propagates recording errors."""
        try:
            safe_identifiers = tuple(
                (self._short(key), value if type(value) is int else self._short(value))
                for key, value in list((identifiers or {}).items())[:MAX_EVENT_IDENTIFIERS]
                if type(value) is int or isinstance(value, str)
            )
            safe = tuple(
                (self._short(key), value if isinstance(value, (int, float, bool)) or value is None
                 else self._short(value))
                for key, value in list((metadata or {}).items())[:MAX_EVENT_METADATA_ITEMS]
            )
            item = RuntimeEvent(
                self._clock().isoformat(), self._monotonic(), self._short(component),
                self._short(operation), self._short(status), self._short(severity),
                None if source is None else self._short(source), duration_ms,
                None if error is None else self._short(error), safe_identifiers, safe,
            )
            with self._lock:
                if self._final is None:
                    self._events.append(item)
        except Exception:
            return

    def increment(self, name: str, amount: int = 1, *, dimension: tuple[str, str] | None = None) -> None:
        if name not in _COUNTERS or type(amount) is not int or amount < 0:
            return
        with self._lock:
            if self._final is None:
                self._counters[name] += amount
                if dimension is not None and dimension[0] in self._dimensions:
                    self._dimensions[dimension[0]][self._short(dimension[1])] += amount

    def provider_completed(self, provider: str, model: str | None, stage: str, *,
                           input_tokens: int = 0, cached_input_tokens: int = 0,
                           cache_write_tokens: int = 0,
                           output_tokens: int = 0, total_tokens: int | None = None,
                           duration_ms: int = 0, usage_available: bool = True,
                           protocol: str | None = None, endpoint: str | None = None,
                           returned_model: str | None = None,
                           raw_usage: Mapping[str, int | None] | None = None) -> None:
        """Compatibility entry point for an accepted provider response."""
        values = (input_tokens, cached_input_tokens, cache_write_tokens,
                  output_tokens, duration_ms)
        if any(type(value) is not int or value < 0 for value in values):
            return
        total = input_tokens + output_tokens if total_tokens is None else total_tokens
        if type(total) is not int or total < 0:
            return
        usage = dict(raw_usage) if raw_usage is not None else {
            name: value if usage_available else None for name, value in (
                ("input_tokens", input_tokens), ("cached_input_tokens", cached_input_tokens),
                ("cache_write_tokens", cache_write_tokens), ("output_tokens", output_tokens),
                ("total_tokens", total),
            )
        }
        self.provider_call(
            provider, model, stage, duration_ms=duration_ms, usage=usage,
            usage_available=usage_available, protocol=protocol, endpoint=endpoint,
            returned_model=returned_model,
        )

    def provider_call(self, provider: str, model: str | None, stage: str, *,
                      status: str = "completed", duration_ms: int = 0,
                      usage: Mapping[str, int | None] | None = None,
                      usage_available: bool | None = None,
                      protocol: str | None = None, endpoint: str | None = None,
                      returned_model: str | None = None, error: str | None = None,
                      failure_category: str | None = None, role: str = "cognition",
                      attempt_order: int | None = None, fallback: bool = False,
                      accepted: bool | None = None, http_status: int | None = None,
                      reported_cost_usd: Decimal | None = None) -> None:
        """Account once per attempted outbound call, regardless of output acceptance.

        Reported money and tokens survive rejected output independently. Missing
        cost evidence and unpriceable usage never imply a free call. Adapters
        must verify provider semantics before supplying reported_cost_usd.
        """
        if status not in {"completed", "failed", "cancelled"} or type(duration_ms) is not int or duration_ms < 0:
            return
        if role not in {"cognition", "vision"}:
            return
        counts = {
            name: value if type(value := (usage or {}).get(name)) is int and value >= 0 else None
            for name in (*TOKEN_FIELDS, "reasoning_tokens")
        }
        available = usage_available is not False and all(
            counts[name] is not None for name in ("input_tokens", "output_tokens", "total_tokens")
        )
        missing = {name for name in TOKEN_FIELDS if counts[name] is None}
        input_tokens, cached_input_tokens, cache_write_tokens, output_tokens, total = (
            counts[name] or 0 for name in TOKEN_FIELDS
        )
        key = (role, self._short(provider), self._short(model or "unknown"))
        prefix = "vision_" if role == "vision" else ""
        with self._lock:
            if self._final is not None:
                return
            for name, value in (("provider_requests", 1), ("input_tokens", input_tokens),
                                ("cached_input_tokens", cached_input_tokens),
                                ("cache_write_tokens", cache_write_tokens),
                                ("output_tokens", output_tokens), ("total_tokens", total),
                                ("provider_duration_ms", duration_ms)):
                self._counters[prefix + name] += value
            if status != "completed":
                self._counters[prefix + "provider_failures"] += 1
            self._dimensions[prefix + "provider_models"]["/".join(key[1:])] += 1
            self._dimensions[prefix + "provider_stages"][self._short(stage)] += 1
            bucket = self._provider_usage.setdefault(key, Counter())
            if not available:
                self._missing_usage.add(key)
            self._missing_token_fields.setdefault(key, set()).update(missing)
            reported = parse_usd(reported_cost_usd)
            estimate = None if reported is not None else self._estimate_call(key, counts, available)
            basis = self._record_money(role, reported, estimate)
            record = {
                "reported_cost_usd": usd_string(reported) if reported is not None else None,
                "cost_basis": basis,
                "cost_usd": usd_string(reported if reported is not None else estimate)
                            if basis != "unavailable" else None,
                "role": role,
                "backend": provider, "protocol": protocol, "endpoint": endpoint,
                "requested_model": model, "returned_model": returned_model,
                "stage": stage, "status": status, "duration_ms": duration_ms,
                "usage_available": available, "failure_category": failure_category or error,
                "usage": ({name: counts[name] for name in usage if name in counts}
                          if usage is not None else None),
            }
            if role == "vision":
                record.update(attempt_order=attempt_order, fallback=fallback, accepted=accepted,
                              http_status=http_status if type(http_status) is int and 400 <= http_status <= 599 else None)
            self._provider_calls.append({
                name: self._short(value) if isinstance(value, str) else value
                for name, value in record.items()
            })
            for field, value in (("requests", 1), ("input_tokens", input_tokens),
                                 ("cached_input_tokens", cached_input_tokens),
                                 ("cache_write_tokens", cache_write_tokens),
                                 ("output_tokens", output_tokens),
                                 ("total_tokens", total), ("duration_ms", duration_ms)):
                bucket[field] += value
        self.event(role, "provider_request", status, source=provider,
                   severity="info" if status == "completed" else "error", error=error,
                   duration_ms=duration_ms, metadata={"model": model or "unknown", "stage": stage,
                       "protocol": protocol, "endpoint": endpoint, "returned_model": returned_model,
                       "usage_available": available, "failure_category": failure_category or error})

    def provider_failed(self, provider: str, model: str | None, stage: str, *,
                        duration_ms: int = 0, error: str | None = None,
                        protocol: str | None = None, endpoint: str | None = None,
                        returned_model: str | None = None, status: str = "failed",
                        failure_category: str | None = None,
                        usage: Mapping[str, int | None] | None = None) -> None:
        """Compatibility entry point for a failed or cancelled provider call."""
        self.provider_call(
            provider, model, stage, status=status, duration_ms=duration_ms,
            error=error, protocol=protocol, endpoint=endpoint, returned_model=returned_model,
            failure_category=failure_category, usage=usage,
        )

    def tts_synthesized(self, provider: str, model: str, *, characters: int,
                        duration_ms: int = 0,
                        usage_basis: str = "text_length_estimate") -> None:
        """Record hosted usage once audio is collected, before local playback."""
        if any(type(value) is not int or value < 0
               for value in (characters, duration_ms)) or usage_basis not in {
                   "provider_reported", "text_length_estimate"
               }:
            return
        key = (self._short(provider), self._short(model), usage_basis)
        with self._lock:
            if self._final is not None:
                return
            bucket = self._tts_usage.setdefault(key, Counter())
            bucket.update(syntheses=1, characters=characters, duration_ms=duration_ms)
            rate = self._pricing.character_rates.get(key[:2]) if self._pricing else None
            unit = parse_usd(rate.usd_per_thousand) if rate else None
            with localcontext() as context:
                context.prec = 80
                estimate = parse_usd(Decimal(characters) * unit / 1_000) if unit is not None else None
            self._record_money("tts", None, estimate)
        self.event("voice", "tts_synthesis", "completed", source=provider,
                   duration_ms=duration_ms,
                   metadata={"model": model, "characters": characters,
                             "usage_basis": usage_basis})

    def _estimate_call(self, key, counts, available) -> Decimal | None:
        rate = self._pricing.rates.get(key[1:]) if self._pricing else None
        if rate is None or not available or any(counts[name] is None for name in TOKEN_FIELDS):
            return None
        tokens = counts["input_tokens"]
        cached, writes = counts["cached_input_tokens"], counts["cache_write_tokens"]
        if (cached + writes > tokens or counts["total_tokens"] != tokens + counts["output_tokens"]
                or (rate.max_input_tokens is not None and tokens > rate.max_input_tokens)):
            return None
        units = [parse_usd(value) for value in
                 (rate.input_per_million, rate.cached_input_per_million, rate.output_per_million)]
        write_unit = parse_usd(rate.cache_write_per_million)
        if any(value is None for value in units) or (writes and write_unit is None):
            return None
        with localcontext() as context:
            context.prec = 80
            amount = (Decimal(tokens - cached - writes) * units[0]
                      + Decimal(cached) * units[1] + Decimal(counts["output_tokens"]) * units[2])
            if writes:
                amount += Decimal(writes) * write_unit
            return parse_usd(amount / 1_000_000)

    def _record_money(self, role, reported, estimate) -> str:
        bucket = self._money[role]
        basis = "reported" if reported is not None else "estimated" if estimate is not None else "unavailable"
        if basis == "unavailable":
            bucket["unknown_calls"] += 1
        else:
            amount = reported if reported is not None else estimate
            bucket[basis] = add_usd(bucket[basis], amount)
            bucket[basis + "_calls"] += 1
        return basis

    @staticmethod
    def _money_result(bucket, *, empty_unavailable=False) -> dict[str, object]:
        reported, estimated = bucket["reported"], bucket["estimated"]
        known = add_usd(reported, estimated)
        reported_calls, estimated_calls, unknown = (
            bucket[name + "_calls"] for name in ("reported", "estimated", "unknown"))
        status = ("unavailable" if unknown or (empty_unavailable and reported_calls + estimated_calls == 0)
                  else "mixed" if reported_calls and estimated_calls
                  else "reported" if reported_calls else "estimated")
        return {"status": status,
                "total_usd": usd_string(known) if status != "unavailable" else None,
                # Legacy field retains its original meaning: a complete catalog estimate.
                "estimated_usd": usd_string(known) if status == "estimated" else None,
                "reported_usd": usd_string(reported),
                "catalog_estimated_usd": usd_string(estimated), "known_usd": usd_string(known),
                "reported_calls": reported_calls, "estimated_calls": estimated_calls,
                "unknown_calls": unknown}

    def _cost(self) -> dict[str, object]:
        total = {"reported": Decimal(0), "estimated": Decimal(0),
                 "reported_calls": 0, "estimated_calls": 0, "unknown_calls": 0}
        components = {}
        for role, bucket in self._money.items():
            components[role] = self._money_result(bucket)
            for name, value in bucket.items():
                total[name] = add_usd(total[name], value) if isinstance(value, Decimal) else total[name] + value
        components["tts"]["basis"] = "public_api_character_rate"
        return {**self._money_result(total, empty_unavailable=True),
                "currency": "USD", "pricing_identity": self._pricing.identity if self._pricing else None,
                "components": components}

    def snapshot(self) -> Mapping[str, object]:
        with self._lock:
            data = self._build_snapshot_locked(self._clock(), None, None)
        # JSON round-trip guarantees callers receive no mutable internal objects.
        return MappingProxyType(json.loads(json.dumps(data)))

    def events(self, *, component: str | None = None, severity: str | None = None,
               since_monotonic: float | None = None) -> tuple[RuntimeEvent, ...]:
        if since_monotonic is not None and not isinstance(since_monotonic, (int, float)):
            raise TypeError("since_monotonic must be numeric or None")
        with self._lock:
            return tuple(item for item in self._events
                         if (component is None or item.component == component)
                         and (severity is None or item.severity == severity)
                         and (since_monotonic is None
                              or item.monotonic_seconds >= since_monotonic))

    def recent_events(
        self, *, component: str | None = None, severity: str | None = None,
        since_seconds_ago: float | None = None,
    ) -> tuple[RuntimeEvent, ...]:
        """Return ring-local events using this recorder's monotonic time domain."""
        if (since_seconds_ago is not None
                and (isinstance(since_seconds_ago, bool)
                     or not isinstance(since_seconds_ago, (int, float))
                     or since_seconds_ago < 0)):
            raise ValueError("since_seconds_ago must be a non-negative number or None")
        threshold = (
            None if since_seconds_ago is None
            else self._monotonic() - since_seconds_ago
        )
        return self.events(
            component=component, severity=severity, since_monotonic=threshold
        )

    def _build_snapshot_locked(self, stopped: datetime, status: str | None,
                               shutdown: str | None) -> dict[str, Any]:
        def metric(name, value):
            role = "vision" if name.startswith("vision_") else "cognition"
            field = name.removeprefix("vision_") if role == "vision" else name
            if ((field in TOKEN_FIELDS and any(key[0] == role for key in self._missing_usage))
                    or any(key[0] == role and field in fields for key, fields in self._missing_token_fields.items())):
                return None
            return value

        def usage_for(role):
            return [
                {"provider": provider, "model": model,
                 "usage_available": (role, provider, model) not in self._missing_usage,
                 **{field: None if ((role, provider, model) in self._missing_usage and field.endswith("tokens"))
                    or field in self._missing_token_fields.get((role, provider, model), ())
                    else usage[field] for field in (
                     "requests", "input_tokens", "cached_input_tokens", "cache_write_tokens",
                     "output_tokens", "total_tokens", "duration_ms")}}
                for (kind, provider, model), usage in sorted(self._provider_usage.items()) if kind == role
            ]
        return {
            "schema_version": 6,
            "run": {"run_id": self.run_id, "started_at": self._started_at.isoformat(),
                    "stopped_at": stopped.isoformat() if status else None,
                    "elapsed_seconds": max(0.0, self._monotonic() - self._started_monotonic),
                    "status": status or "running", "shutdown": shutdown},
            "metrics": {name: metric(name, value) for name, value in self._counters.items()},
            "provider_calls": list(self._provider_calls),
            "dimensions": {key: dict(value) for key, value in self._dimensions.items()},
            "provider_usage": usage_for("cognition"),
            "vision_usage": usage_for("vision"),
            "tts_usage": [
                {"provider": provider, "model": model, "usage_basis": usage_basis,
                 **{field: usage[field] for field in
                    ("syntheses", "characters", "duration_ms")}}
                for (provider, model, usage_basis), usage in sorted(self._tts_usage.items())
            ],
            "cost": self._cost(),
        }

    def finalize(self, status: str, *, shutdown: str | None = None,
                 directory: Path | None = None) -> Mapping[str, object]:
        """Freeze exactly once and optionally atomically write ``summary.json``."""
        with self._lock:
            first_finalization = self._final is None
            if self._final is None:
                stopped = self._clock()
                self._events.append(RuntimeEvent(stopped.isoformat(), self._monotonic(),
                                                  "runtime", "run", "stopped"))
                self._final = self._build_snapshot_locked(stopped, status, shutdown)
            result = json.loads(json.dumps(self._final))
        if directory is not None and first_finalization:
            try:
                temporary = directory / ".summary.json.tmp"
                temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                temporary.replace(directory / "summary.json")
            except OSError as error:
                with self._lock:
                    self._summary_persistence = "failed"
                    self._summary_persistence_error = type(error).__name__
            else:
                with self._lock:
                    self._summary_persistence = "written"
        return MappingProxyType(result)

    @property
    def summary_persistence(self) -> tuple[str, str | None]:
        """Return ``(status, bounded error class)`` for the last persistence attempt."""
        with self._lock:
            return self._summary_persistence, self._summary_persistence_error

    def banner(self, snapshot: Mapping[str, object] | None = None) -> str:
        data = snapshot or self.snapshot()
        run, metrics, cost = data["run"], data["metrics"], data["cost"]
        # Historical summaries predate some role counters. Do not invent zeros.
        metrics = {**dict.fromkeys(_COUNTERS, "unavailable"), **metrics}
        seconds = int(run["elapsed_seconds"])
        duration = f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
        title = f" {self.run_id or 'RUNTIME'} SUMMARY "
        line = "=" * 64
        value = cost["estimated_usd"] if cost["status"] == "estimated" else "unavailable"
        components = cost.get("components", {})
        def component_cost(role):
            item = components.get(role, {})
            amount = item.get("total_usd", item.get("estimated_usd"))
            return amount if amount is not None else "unavailable"
        cognition_cost = component_cost("cognition")
        tts_cost = component_cost("tts")
        total = cost.get("total_usd", cost.get("estimated_usd"))
        def tokens(name: str) -> object:
            return metrics[name] if metrics[name] is not None else "unavailable"
        return "\n".join((line, title.center(64, "="), "", "Runtime",
            f"  duration:             {duration}", f"  status:               {run['status']}", "",
            "Cognition", f"  provider_requests:    {metrics['provider_requests']}",
            f"  input_tokens:         {tokens('input_tokens')}",
            f"  cached_input_tokens:  {tokens('cached_input_tokens')}",
            f"  output_tokens:        {tokens('output_tokens')}",
            f"  cognition_cost_usd:   {cognition_cost}",
            f"  tts_cost_usd:         {tts_cost}",
            f"  estimated_cost_usd:   {value}",
            f"  total_cost_usd:       {total if total is not None else 'unavailable'}",
            f"  cost_basis:           {cost['status']}",
            f"  reported_cost_usd:    {cost.get('reported_usd', 'unavailable')}",
            f"  catalog_est_cost_usd: {cost.get('catalog_estimated_usd', 'unavailable')}",
            f"  known_cost_usd:       {cost.get('known_usd', 'unavailable')}",
            f"  unknown_cost_calls:   {cost.get('unknown_calls', 'unavailable')}", "", "Voice",
            f"  wake_captures:        {metrics['wake_captures']}",
            f"  stt_captures:         {metrics['stt_captures']}",
            f"  voice_turns:          {metrics['voice_turns']}",
            f"  tts_generations:      {metrics['tts_generations']}",
            f"  tts_characters:       {metrics['tts_characters']}",
            f"  tts_fallbacks:        {metrics['tts_fallbacks']}",
            f"  voice_failures:       {metrics['voice_failures']}", "", "Perception",
            f"  camera_captures:      {metrics['camera_captures']}",
            f"  vision_acquisitions:  {metrics['vision_acquisitions']}",
            f"  vision_requests:      {metrics['vision_provider_requests']}",
            f"  vision_fallbacks:     {metrics['vision_fallbacks']}",
            f"  vision_input_tokens:  {tokens('vision_input_tokens')}",
            f"  vision_output_tokens: {tokens('vision_output_tokens')}",
            f"  vision_cost_usd:      {component_cost('vision')}",
            "", "Autonomy",
            f"  attention_episodes:   {metrics['attention_episodes_started']}",
            f"  job_runs_started:     {metrics['job_runs_started']}",
            f"  job_runs_completed:   {metrics['job_runs_completed']}",
            f"  automatic_job_work:   {metrics['automatic_job_work']}", "", "Errors",
            f"  provider_failures:    {metrics['provider_failures']}",
            f"  runtime_errors:       {metrics['runtime_errors']}", "", line))
