"""Offline power-transition fixture; all Job and tool semantics stay in production."""

from __future__ import annotations

import asyncio
import json

from embodied_runtime.app import CurrentJobRun, RobotApplication
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.interaction import (
    InteractionChannel, OperatorMessage, OperatorMessageSink,
)
from embodied_runtime.jobs import (
    JobContinuationReadiness, JobReadinessEventType, JobRunStatus,
)
from embodied_runtime.tasks import TaskStatus

from .recording import RecordingCognitionBackend, ToolTraceEntry


class _BatteryHardware(VirtualHardwareBackend):
    # Same mutable voltage seam used by the power Job activation tests.
    capabilities = ("battery_voltage",)

    def __init__(self) -> None:
        super().__init__()
        self.voltage = 7.3

    def read_battery_voltage_v(self) -> float:
        return self.voltage


class _OperatorSink(OperatorMessageSink):
    channel = InteractionChannel.CONSOLE

    def __init__(self) -> None:
        self.messages: list[OperatorMessage] = []

    async def deliver(self, message: OperatorMessage) -> None:
        self.messages.append(message)


def _calls(trace: list[ToolTraceEntry], name: str, **arguments: object):
    for item in trace:
        if item.name != name:
            continue
        try:
            supplied = json.loads(item.arguments)
        except (TypeError, ValueError):
            continue
        if isinstance(supplied, dict) and all(
            supplied.get(key) == value for key, value in arguments.items()
        ):
            yield item


class PowerRecoveryFixture:
    def __init__(self) -> None:
        self.hardware = _BatteryHardware()
        self.sink = _OperatorSink()
        self.evidence: dict[str, object] = {}
        self.checks = {"continuity": False, "verified_completion": False}

    async def resume(
        self, app: RobotApplication, binding: CurrentJobRun,
        recorder: RecordingCognitionBackend,
    ) -> int:
        """Sample unchanged power, then recover and offer the exact parked Run.

        Hold ordinary operator fairness during recovery publication so the wake
        can be observed before admission consumes it. No manual Job work, direct
        state replacement, fabricated event, or new occurrence performs resumption.
        """
        run_id, task_id = binding.run.id, binding.task.id
        initial_trace = list(recorder.tool_trace)
        continuation = app.job_continuation
        context = app._context_for_run(run_id)
        waiting = (
            continuation is not None
            and continuation.readiness is JobContinuationReadiness.WAIT_FOR_EVENT
            and continuation.event_type is JobReadinessEventType.POWER_RECOVERED
            and continuation.run_id == run_id and continuation.task_id == task_id
            and context is not None and context.execution_state == "parked"
            and context.binding.task.status is TaskStatus.PAUSED
        )
        self.evidence.update(
            initial_run_id=run_id, initial_task_id=str(task_id),
            parked_run_id=None if continuation is None else continuation.run_id,
            parked_task_id=None if continuation is None else str(continuation.task_id),
            parked_for_power_recovered=waiting,
            parked_task_status=None if context is None else context.binding.task.status.value,
            prior_summary=None if continuation is None else continuation.last_summary,
            initial_trace_end=len(initial_trace),
        )
        request_count = len(recorder.requests)
        await app._power_monitor.sample_once()  # unchanged warning; emits no recovery
        if waiting:
            app._offer_job_continuation()
        await asyncio.sleep(0)
        durable = app.jobs.get_run(run_id)
        open_under_warning = (
            app.runtime_state.power.condition.value == "attention"
            and durable is not None and durable.status is JobRunStatus.RUNNING
        )
        self.evidence.update(
            warning_condition=app.runtime_state.power.condition.value,
            warning_voltage_v=app.runtime_state.power.battery_voltage_v,
            warning_run_status=None if durable is None else durable.status.value,
            no_work_before_recovery=len(recorder.requests) == request_count,
        )
        # This gate is also used in the existing exact-run power-event tests.
        app.episode_coordinator._operator_waiters += 1
        try:
            self.hardware.voltage = 7.8
            await app._power_monitor.sample_once()
            if waiting:
                # Only local event dispatch is bounded here, never provider work.
                async with asyncio.timeout(5):
                    while not app.job_continuation.event_satisfied:
                        await asyncio.sleep(0)
            wake = None if app.job_continuation is None else app.job_continuation.wake_event
            self.evidence.update(
                recovery_condition=app.runtime_state.power.condition.value,
                recovery_voltage_v=app.runtime_state.power.battery_voltage_v,
                wake_event=None if wake is None else wake.event_type.value,
                wake_voltage_v=None if wake is None else wake.battery_voltage_v,
            )
        finally:
            app.episode_coordinator._operator_waiters -= 1

        episodes = 0
        if waiting:
            app._offer_job_continuation()
            context = app._context_for_run(run_id)
            work = None if context is None else context.active_work_task
            if work is not None:
                self.evidence.update(
                    resumed_run_id=context.run_id, resumed_task_id=str(context.task_id),
                    resumed_task_status=context.binding.task.status.value,
                )
                episodes = 1
                await work

        resumed_trace = recorder.tool_trace[len(initial_trace):]
        messages = list(_calls(initial_trace, "address_operator"))
        waits = list(_calls(
            initial_trace, "report_job_outcome", disposition="continue",
            readiness="wait_for_event", event_type="power_recovered",
        ))
        unparsed_outcomes = sum(
            item.name == "report_job_outcome"
            and not list(_calls([item], "report_job_outcome"))
            for item in initial_trace
        )
        self.evidence.update(
            operator_request_attempts_before_recovery=len(messages),
            operator_request_attempts_after_recovery=sum(
                item.name == "address_operator" for item in resumed_trace
            ),
            operator_requests_delivered=len(self.sink.messages),
            unparsed_initial_outcomes=unparsed_outcomes,
        )
        continuity = (
            waiting and len(messages) == 1 and messages[0].status == "applied"
            and len(self.sink.messages) == 1
            and any(item.status == "accepted" for item in waits)
            and bool(self.evidence["prior_summary"])
            and self.evidence["no_work_before_recovery"]
            and self.evidence.get("resumed_run_id") == run_id
            and self.evidence.get("resumed_task_id") == str(task_id)
            and self.evidence.get("resumed_task_status") == "running"
            and self.evidence["wake_event"] == "power_recovered"
            and not any(item.name == "address_operator" for item in resumed_trace)
            and len(app.jobs.list_runs(binding.job.id)) == 1
        )
        durable = app.jobs.get_run(run_id)
        self.evidence["final_run_status"] = None if durable is None else durable.status.value
        premature = list(_calls(initial_trace, "report_job_outcome", disposition="completed"))
        self.evidence["premature_completion_attempts"] = len(premature)
        self.checks.update(
            continuity=bool(continuity),
            verified_completion=bool(
                open_under_warning and not premature and not unparsed_outcomes
                and self.evidence["recovery_condition"] == "normal"
                and self.evidence.get("resumed_run_id") == run_id
                and any(item.status == "accepted" for item in _calls(
                    resumed_trace, "report_job_outcome", disposition="completed",
                ))
                and durable is not None and durable.status is JobRunStatus.COMPLETED
            ),
        )
        return episodes

    def failure_reasons(self) -> list[str]:
        reasons = []
        if not self.checks["continuity"]:
            reasons.append(
                "continuity: expected one delivered operator request, an exact-run "
                "power recovery wait and wake with prior context, and no repeated request"
            )
        if not self.checks["verified_completion"]:
            reasons.append(
                "verified_completion: expected an open Run under warning and resumed "
                "completion on current recovery evidence"
            )
        if self.evidence.get("operator_request_attempts_after_recovery"):
            reasons.append("continuity: operator request repeated after recovery")
        if self.evidence.get("premature_completion_attempts"):
            reasons.append("verified_completion: premature completion attempted while power warning active")
        if self.evidence.get("unparsed_initial_outcomes"):
            reasons.append("verified_completion: initial outcome arguments could not be scored")
        return reasons
