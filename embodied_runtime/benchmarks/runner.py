"""Small provider-neutral runner for fresh, ephemeral cognition trials."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.cognition import TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    FilesystemJobWorkspaceStore, JobContinuation, JobProgressUpdate, JobRunStatus,
    SQLiteJobStore,
)
from embodied_runtime.platform import PlatformSnapshot
from embodied_runtime.observability import RunObservability
from embodied_runtime.pricing import BUILT_IN_PRICING
from embodied_runtime.profile import RobotProfile
from .models import BenchmarkReport, BenchmarkTrialResult, TrialMetrics
from .recording import RecordingCognitionBackend
from .scenario import ACQUISITION_TOOL_NAMES, SCENARIO_ID, get_scenario


class _FixedPlatform:
    def snapshot(self) -> PlatformSnapshot:
        return PlatformSnapshot(
            hostname="benchmark", system="BenchmarkOS", release="1",
            machine="virtual", python_version="3.13", model="virtual",
            uptime_seconds=3600.0, load_averages=(0.0, 0.0, 0.0),
            memory_total_bytes=512 * 1024 * 1024,
            memory_available_bytes=384 * 1024 * 1024,
            cpu_temperature_celsius=40.0, captured_monotonic=1.0,
        )


class _Heartbeat:
    def __init__(self) -> None:
        self._pulses: asyncio.Queue[None] = asyncio.Queue()

    async def sleep(self, _delay: float) -> None:
        await self._pulses.get()

    def pulse(self) -> None:
        self._pulses.put_nowait(None)


async def _wait_for_continuation_acceptance(
    app: RobotApplication, run_id: int, previous_remaining: int,
) -> JobContinuation | None:
    """Wait until one pulse is accepted or the occurrence becomes non-runnable."""
    while True:
        durable = app.jobs.get_run(run_id) if app.jobs is not None else None
        continuation = app.job_continuation
        if durable is None or durable.status is not JobRunStatus.RUNNING:
            return None
        if continuation is None or continuation.state.value == "awaiting_operator":
            return None
        if continuation.automatic_steps_remaining < previous_remaining:
            return continuation
        await asyncio.sleep(0)


async def _wait_for_continuation_settlement(
    app: RobotApplication, run_id: int, accepted: JobContinuation,
) -> None:
    """Wait until accepted work publishes its terminal or next-continuation state."""
    while True:
        durable = app.jobs.get_run(run_id) if app.jobs is not None else None
        continuation = app.job_continuation
        if durable is None or durable.status is not JobRunStatus.RUNNING:
            return
        if continuation is not accepted:
            return
        await asyncio.sleep(0)


def _metric_delta(before: object, after: object, name: str) -> int:
    old = before.get("metrics", {}).get(name, 0)  # type: ignore[union-attr]
    new = after.get("metrics", {}).get(name, 0)  # type: ignore[union-attr]
    return int(new) - int(old)


def _latest_response(recorder: RecordingCognitionBackend) -> str:
    for request in reversed(recorder.requests):
        if request.response_text:
            return request.response_text
    return ""


def _cost_delta(before: object, after: object) -> str | None:
    """Subtract already-priced snapshots, retaining observability's precision."""
    old = before.get("cost", {})  # type: ignore[union-attr]
    new = after.get("cost", {})  # type: ignore[union-attr]
    if old.get("status") != "estimated" or new.get("status") != "estimated":
        return None
    try:
        delta = Decimal(new["estimated_usd"]) - Decimal(old["estimated_usd"])
    except (InvalidOperation, KeyError, TypeError):
        return None
    if delta < 0:
        return None
    return str(delta.quantize(Decimal("0.000001")))


async def run_trial(
    backend: TextCognitionBackend, model: str, repetition: int,
    *, scenario_id: str = SCENARIO_ID,
) -> BenchmarkTrialResult:
    """Run one entirely fresh trial; the application owns and closes both stores."""
    scenario = get_scenario(scenario_id)
    started = perf_counter()
    recorder = RecordingCognitionBackend(backend)
    heartbeat = _Heartbeat()
    app: RobotApplication | None = None
    status = disposition = continuation_state = None
    reasons: list[str] = []
    error_text = None
    before: object = {"metrics": {}}
    after: object = {"metrics": {}}
    episodes = 0
    continuation_count = 0
    historical_content_version: str | None = None
    seeded_content_version: str | None = None
    seeded_artifact_path: str | None = None
    with TemporaryDirectory(prefix="mira-cognition-benchmark-") as directory:
        root = Path(directory)
        jobs = SQLiteJobStore(root / "jobs.sqlite3")
        workspaces = FilesystemJobWorkspaceStore(root / "workspaces")
        observability = RunObservability(pricing=BUILT_IN_PRICING)
        app = RobotApplication(
            RobotProfile("mira-benchmark", "Mira"), VirtualHardwareBackend(),
            ApplicationOptions(
                initiative_enabled=True, initiative_goal_closure_enabled=True,
                jobs_auto_continue=True, jobs_heartbeat_seconds=1,
                jobs_max_auto_steps=3,
            ),
            platform_provider=_FixedPlatform(), cognition_backend=recorder,
            self_inspector=(
                scenario.self_inspector_factory()
                if scenario.self_inspector_factory is not None else None
            ),
            job_store=jobs, job_workspace_store=workspaces,
            job_continuation_sleep=heartbeat.sleep,
            wall_clock=lambda: datetime(2026, 10, 4, 12, tzinfo=UTC),
            observability=observability,
        )
        run_id: int | None = None
        try:
            await app.start()
            job = jobs.create_job(scenario.job_title, scenario.description)
            if scenario.historical_baseline is not None:
                seeded_baseline = workspaces.write(
                    job.id, "communication_baseline.txt", "create",
                    scenario.historical_baseline,
                )
                historical_content_version = seeded_baseline.content_version
            binding = app.start_job_run(job.id)
            run_id = binding.run.id
            if scenario.seeded_artifact is not None:
                seeded_artifact_path, content = scenario.seeded_artifact
                seeded = workspaces.write(job.id, seeded_artifact_path, "create", content)
                seeded_content_version = seeded.content_version
            if scenario.initial_progress_counter is not None:
                progress = app.job_progress
                if progress is None:
                    raise RuntimeError("current Job progress was not initialized")
                # Benchmark setup uses the same exact-occurrence production value and
                # increment operation that accepted runtime evidence commits.
                app._job_progress = progress.increment(JobProgressUpdate(
                    scenario.initial_progress_counter, "effect_1",
                ))
            # Snapshot after prepare/start so prewarm is deliberately excluded.
            before = app.observability.snapshot()
            episodes = 1
            outcome = await app.work_current_job_once()
            disposition = outcome.disposition.value
            while True:
                durable = jobs.get_run(run_id)
                if durable is None or durable.status not in (
                    JobRunStatus.PENDING, JobRunStatus.RUNNING,
                ):
                    break
                continuation = app.job_continuation
                if continuation is None or continuation.state.value == "awaiting_operator":
                    break
                heartbeat.pulse()
                accepted = await _wait_for_continuation_acceptance(
                    app, run_id, continuation.automatic_steps_remaining,
                )
                if accepted is None:
                    continue
                episodes += 1
                continuation_count += 1
                await _wait_for_continuation_settlement(app, run_id, accepted)
            durable = jobs.get_run(run_id)
            status = None if durable is None else durable.status.value
            current = app.job_continuation
            continuation_state = None if current is None else current.state.value
            if status != JobRunStatus.COMPLETED.value:
                reasons.append(f"Job ended with status {status or 'unknown'}")
            if continuation_state == "awaiting_operator" and status != "completed":
                if current is not None and current.automatic_steps_remaining == 0:
                    reasons.append("continuation budget exhausted")
                else:
                    reasons.append("Job remained awaiting_operator")
            forbidden = [
                item.name for item in recorder.tool_trace
                if item.name not in {
                    "workspace_list", "workspace_read", "workspace_write",
                    "inspect_self", "search_findings", "publish_finding",
                    "report_job_outcome",
                }
            ]
            if forbidden:
                reasons.append("forbidden effect occurred: " + ", ".join(forbidden))
            reasons.extend(scenario.evaluate_trace(
                tuple(recorder.tool_trace),
                historical_content_version=historical_content_version,
                seeded_content_version=seeded_content_version,
                final_seeded_content_version=(
                    None if seeded_artifact_path is None else
                    (artifact.content_version if (artifact := workspaces.read(
                        job.id, seeded_artifact_path,
                    )) is not None else None)
                ),
                continuation_count=continuation_count,
            ))
        except Exception as error:
            error_text = f"{type(error).__name__}: {str(error)[:500]}"
            reasons.append("trial error")
        finally:
            if app is not None:
                if run_id is not None:
                    durable = jobs.get_run(run_id)
                    status = None if durable is None else durable.status.value
                    current = app.job_continuation
                    continuation_state = (
                        None if current is None else current.state.value
                    )
                after = app.observability.snapshot()
                try:
                    await app.stop()
                except Exception as cleanup_error:
                    if error_text is None:
                        error_text = (
                            f"cleanup {type(cleanup_error).__name__}: "
                            f"{str(cleanup_error)[:500]}"
                        )
                        reasons.append("cleanup error")

    for item in reversed(recorder.tool_trace):
        if item.name == "report_job_outcome" and item.status == "accepted":
            try:
                proposed = json.loads(item.arguments).get("disposition")
            except (ValueError, AttributeError):
                pass
            else:
                if isinstance(proposed, str):
                    disposition = proposed
            break
    names = [item.name for item in recorder.tool_trace]
    acquisitions = sum(name in ACQUISITION_TOOL_NAMES for name in names)
    effects = sum(name in {"workspace_write", "publish_finding"} for name in names)
    metrics = TrialMetrics(
        provider_requests=_metric_delta(before, after, "provider_requests"),
        input_tokens=_metric_delta(before, after, "input_tokens"),
        cached_input_tokens=_metric_delta(before, after, "cached_input_tokens"),
        output_tokens=_metric_delta(before, after, "output_tokens"),
        job_work_episodes=episodes, continuation_count=continuation_count,
        acquisition_tool_calls=acquisitions, effect_tool_calls=effects,
    )
    estimated_cost_usd = _cost_delta(before, after)
    return BenchmarkTrialResult(
        scenario_id, backend.identifier, model, repetition, not reasons,
        tuple(reasons), error_text, round(perf_counter() - started, 6),
        estimated_cost_usd, BUILT_IN_PRICING.identity, status,
        disposition, continuation_state, _latest_response(recorder), metrics,
        tuple(recorder.requests), tuple(recorder.tool_trace),
    )


async def run_benchmark(
    backend_factory: Callable[[str], TextCognitionBackend], models: list[str],
    repeat: int, *, scenario_id: str = SCENARIO_ID,
    progress: Callable[[str, int, int, BenchmarkTrialResult | None], None] | None = None,
) -> BenchmarkReport:
    trials = []
    for model in models:
        for repetition in range(1, repeat + 1):
            if progress is not None:
                progress(model, repetition, repeat, None)
            trial = await run_trial(
                backend_factory(model), model, repetition, scenario_id=scenario_id,
            )
            trials.append(trial)
            if progress is not None:
                progress(model, repetition, repeat, trial)
    return BenchmarkReport(
        datetime.now(UTC).isoformat(), scenario_id, tuple(trials),
        BUILT_IN_PRICING.identity,
    )


def _estimated_total(trials: list[BenchmarkTrialResult]) -> str | None:
    amounts = [Decimal(trial.estimated_cost_usd) for trial in trials
               if trial.estimated_cost_usd is not None]
    if not amounts:
        return None
    return str(sum(amounts, Decimal(0)).quantize(Decimal("0.000001")))


def render_report(report: BenchmarkReport) -> str:
    lines = ["Mira Cognition Benchmark", f"scenario: {report.scenario_id}", ""]
    for model in dict.fromkeys(trial.model for trial in report.trials):
        trials = [trial for trial in report.trials if trial.model == model]
        passed = sum(trial.passed for trial in trials)
        lines.extend((f"model: {model}", f"PASS {passed}/{len(trials)}",
                      "trial result episodes provider_req acquisitions effects "
                      "output_tokens est_cost_usd duration"))
        for trial in trials:
            metric = trial.metrics
            lines.append(
                f"{trial.repetition:<5} {'PASS' if trial.passed else 'FAIL':<6} "
                f"{metric.job_work_episodes:<8} {metric.provider_requests:<12} "
                f"{metric.acquisition_tool_calls:<12} {metric.effect_tool_calls:<7} "
                f"{metric.output_tokens:<13} "
                f"{('$' + trial.estimated_cost_usd) if trial.estimated_cost_usd is not None else 'unavailable':<12} "
                f"{trial.wall_duration_seconds:.1f}s"
            )
        model_total = _estimated_total(trials)
        lines.append(
            "estimated model cost: "
            + (f"${model_total}" if model_total is not None else "unavailable")
        )
        failures = [trial for trial in trials if not trial.passed]
        if failures:
            lines.append("Failure reasons")
            for trial in failures:
                lines.append(f"trial {trial.repetition}: {'; '.join(trial.failure_reasons)}")
                lines.append("  tool trace: " + ", ".join(
                    f"{item.ordinal}:{item.name}"
                    f"[{item.status or item.error or 'unknown'}]"
                    for item in trial.tool_trace
                ))
        lines.append("")
    report_total = _estimated_total(list(report.trials))
    lines.append(
        "estimated report cost: "
        + (f"${report_total}" if report_total is not None else "unavailable")
    )
    return "\n".join(lines).rstrip() + "\n"
