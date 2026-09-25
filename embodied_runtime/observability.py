"""Bounded, runtime-owned accounting for one process run.

This module deliberately stores operational metadata only.  Callers must not put
prompts, transcripts, media, credentials, or provider response bodies in events.
"""

from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
from threading import Lock
from time import monotonic
from types import MappingProxyType
from typing import Any


MAX_EVENT_STRING = 160
MAX_EVENT_METADATA_ITEMS = 12
MAX_EVENT_IDENTIFIERS = 8
DEFAULT_EVENT_CAPACITY = 200


@dataclass(frozen=True, slots=True)
class TokenPrice:
    """USD per million tokens; cached input is priced independently."""

    input_per_million: Decimal
    cached_input_per_million: Decimal
    output_per_million: Decimal
    cache_write_per_million: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PricingCatalog:
    rates: Mapping[tuple[str, str], TokenPrice]
    identity: str


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
    "voice_failures", "camera_captures", "camera_failures",
    "vision_acquisitions", "vision_failures", "attention_episodes_started",
    "attention_episodes_completed", "operator_attention_episodes",
    "automatic_attention_episodes", "job_runs_started", "job_runs_completed",
    "job_runs_failed", "job_runs_stopped", "manual_job_work",
    "automatic_job_work", "continuations_accepted", "runtime_errors",
    "interruptions", "resource_failures",
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
            "tts_providers": Counter(), "attention_sources": Counter(),
            "attention_completion_reasons": Counter(), "job_work_outcomes": Counter(),
        }
        self._provider_usage: dict[tuple[str, str], Counter[str]] = {}
        self._unpriceable_usage: set[tuple[str, str]] = set()
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
                           duration_ms: int = 0, usage_available: bool = True) -> None:
        values = (input_tokens, cached_input_tokens, cache_write_tokens,
                  output_tokens, duration_ms)
        if any(type(value) is not int or value < 0 for value in values):
            return
        total = input_tokens + output_tokens if total_tokens is None else total_tokens
        if type(total) is not int or total < 0:
            return
        key = (self._short(provider), self._short(model or "unknown"))
        with self._lock:
            if self._final is not None:
                return
            for name, value in (("provider_requests", 1), ("input_tokens", input_tokens),
                                ("cached_input_tokens", cached_input_tokens),
                                ("cache_write_tokens", cache_write_tokens),
                                ("output_tokens", output_tokens), ("total_tokens", total),
                                ("provider_duration_ms", duration_ms)):
                self._counters[name] += value
            self._dimensions["provider_models"]["/".join(key)] += 1
            self._dimensions["provider_stages"][self._short(stage)] += 1
            bucket = self._provider_usage.setdefault(key, Counter())
            if (not usage_available
                    or cached_input_tokens + cache_write_tokens > input_tokens
                    or total != input_tokens + output_tokens):
                self._unpriceable_usage.add(key)
            for field, value in (("requests", 1), ("input_tokens", input_tokens),
                                 ("cached_input_tokens", cached_input_tokens),
                                 ("cache_write_tokens", cache_write_tokens),
                                 ("output_tokens", output_tokens),
                                 ("total_tokens", total), ("duration_ms", duration_ms)):
                bucket[field] += value
        self.event("cognition", "provider_request", "completed", source=provider,
                   duration_ms=duration_ms, metadata={"model": model or "unknown", "stage": stage})

    def provider_failed(self, provider: str, model: str | None, stage: str, *,
                        duration_ms: int = 0, error: str | None = None) -> None:
        self.increment("provider_failures")
        self.event("cognition", "provider_request", "failed", severity="error",
                   source=provider, duration_ms=duration_ms, error=error,
                   metadata={"model": model or "unknown", "stage": stage})

    def _cost(self) -> dict[str, object]:
        if not self._provider_usage:
            return {"status": "unavailable", "estimated_usd": None,
                    "pricing_identity": self._pricing.identity if self._pricing else None}
        if self._pricing is None:
            return {"status": "unavailable", "estimated_usd": None, "pricing_identity": None}
        total = Decimal(0)
        for (provider, model), usage in self._provider_usage.items():
            if (provider, model) in self._unpriceable_usage:
                return {"status": "unavailable", "estimated_usd": None,
                        "pricing_identity": self._pricing.identity}
            rate = self._pricing.rates.get((provider, model))
            if rate is None:
                return {"status": "unavailable", "estimated_usd": None,
                        "pricing_identity": self._pricing.identity}
            cached = usage["cached_input_tokens"]
            cache_write = usage["cache_write_tokens"]
            uncached = usage["input_tokens"] - cached - cache_write
            if uncached < 0 or (cache_write and rate.cache_write_per_million is None):
                return {"status": "unavailable", "estimated_usd": None,
                        "pricing_identity": self._pricing.identity}
            total += Decimal(uncached) * rate.input_per_million / Decimal(1_000_000)
            total += Decimal(cached) * rate.cached_input_per_million / Decimal(1_000_000)
            total += Decimal(usage["output_tokens"]) * rate.output_per_million / Decimal(1_000_000)
            if cache_write:
                total += Decimal(cache_write) * rate.cache_write_per_million / Decimal(1_000_000)
        return {"status": "estimated", "estimated_usd": str(total.quantize(Decimal("0.000001"))),
                "pricing_identity": self._pricing.identity}

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

    def _build_snapshot_locked(self, stopped: datetime, status: str | None,
                               shutdown: str | None) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run": {"run_id": self.run_id, "started_at": self._started_at.isoformat(),
                    "stopped_at": stopped.isoformat() if status else None,
                    "elapsed_seconds": max(0.0, self._monotonic() - self._started_monotonic),
                    "status": status or "running", "shutdown": shutdown},
            "metrics": dict(self._counters),
            "dimensions": {key: dict(value) for key, value in self._dimensions.items()},
            "provider_usage": [
                {"provider": provider, "model": model,
                 **{field: usage[field] for field in (
                     "requests", "input_tokens", "cached_input_tokens",
                     "cache_write_tokens", "output_tokens", "total_tokens",
                     "duration_ms")}}
                for (provider, model), usage in sorted(self._provider_usage.items())
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
        seconds = int(run["elapsed_seconds"])
        duration = f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
        title = f" {self.run_id or 'RUNTIME'} SUMMARY "
        line = "=" * 64
        value = cost["estimated_usd"] if cost["status"] == "estimated" else "unavailable"
        return "\n".join((line, title.center(64, "="), "", "Runtime",
            f"  duration:             {duration}", f"  status:               {run['status']}", "",
            "Cognition", f"  provider_requests:    {metrics['provider_requests']}",
            f"  input_tokens:         {metrics['input_tokens']}",
            f"  cached_input_tokens:  {metrics['cached_input_tokens']}",
            f"  output_tokens:        {metrics['output_tokens']}",
            f"  estimated_cost_usd:   {value}", "", "Voice",
            f"  wake_captures:        {metrics['wake_captures']}",
            f"  stt_captures:         {metrics['stt_captures']}",
            f"  voice_turns:          {metrics['voice_turns']}",
            f"  tts_generations:      {metrics['tts_generations']}", "", "Perception",
            f"  camera_captures:      {metrics['camera_captures']}",
            f"  vision_acquisitions:  {metrics['vision_acquisitions']}", "", "Autonomy",
            f"  attention_episodes:   {metrics['attention_episodes_started']}",
            f"  job_runs_started:     {metrics['job_runs_started']}",
            f"  job_runs_completed:   {metrics['job_runs_completed']}",
            f"  automatic_job_work:   {metrics['automatic_job_work']}", "", "Errors",
            f"  provider_failures:    {metrics['provider_failures']}",
            f"  runtime_errors:       {metrics['runtime_errors']}", "", line))
