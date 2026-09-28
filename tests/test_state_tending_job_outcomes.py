import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import (
    JOB_OUTCOME_EVALUATION_REQUEST, REPORT_JOB_OUTCOME_TOOL,
    ApplicationOptions, RobotApplication,
)
from embodied_runtime.attention import InitiativeOutcome
from embodied_runtime.cognition import CognitionToolCall, InitiativeEffectOutcome, TextCognitionBackend
from embodied_runtime.events import PowerRecovered
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    JobContinuationReadiness, JobContinuationState, JobReadinessEventType, JobRunStatus,
    JobTriggerType, JobWorkDisposition, SQLiteJobStore,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.state import PowerCondition, PowerState
from embodied_runtime.tasks import TaskStatus
from tests.test_job_execution import Platform


class CorrectingBackend(TextCognitionBackend):
    identifier = "state-tending-test"

    def __init__(self, proposals, before_outcome=None):
        self.proposals = proposals
        self.before_outcome = before_outcome
        self.results = []
        self.instructions = None

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            if self.before_outcome is not None:
                self.before_outcome()
            self.instructions = instructions
            for proposal in self.proposals:
                proposal = {"report": None, "progress_update": None, **proposal}
                self.results.append(await tool_executor(CognitionToolCall(
                    REPORT_JOB_OUTCOME_TOOL.name, json.dumps(proposal))))
        return "bounded work"


class GroupedOutcomeBackend(CorrectingBackend):
    """Submit one bounded group of tool calls per outcome evaluation."""

    def __init__(self, groups, before_outcome=None):
        super().__init__((), before_outcome=before_outcome)
        self.groups = iter(groups)

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            if self.before_outcome is not None:
                self.before_outcome()
            for proposal in next(self.groups):
                proposal = {"report": None, "progress_update": None, **proposal}
                self.results.append(await tool_executor(CognitionToolCall(
                    REPORT_JOB_OUTCOME_TOOL.name, json.dumps(proposal))))
        return "bounded work"


class StateTendingJobOutcomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def app(self, backend, *, auto_continue=True):
        async def sleep_forever(_delay):
            await asyncio.Event().wait()
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True,
                               initiative_goal_closure_enabled=True,
                               jobs_auto_continue=auto_continue),
            platform_provider=Platform(), cognition_backend=backend,
            job_store=self.store,
            wall_clock=lambda: datetime(2026, 9, 28, tzinfo=UTC),
            job_scheduler_sleep=sleep_forever,
            job_continuation_sleep=sleep_forever,
            power_monitor_sleep=sleep_forever,
        )

    @staticmethod
    async def drain():
        for _ in range(16):
            await asyncio.sleep(0)

    async def test_power_completion_is_corrected_to_exact_recovery_wait(self):
        backend = CorrectingBackend((
            {"disposition": "completed", "summary": "done", "readiness": None,
             "delay_seconds": None, "event_type": None},
            {"disposition": "continue", "summary": "waiting",
             "readiness": "wait_for_event", "delay_seconds": None,
             "event_type": "power_recovered"},
        ))
        job = self.store.create_job("Tend power")
        self.store.set_trigger(job.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
        app = self.app(backend)
        await app.start()
        await app._power_monitor.stop()
        app._replace_power_state(PowerState(
            7.2, PowerCondition.ATTENTION, datetime.now(UTC)))
        backend.before_outcome = lambda: app._replace_power_state(PowerState(
            7.2, PowerCondition.ATTENTION, datetime.now(UTC)))
        binding = app.start_job_run(job.id)
        self.assertEqual(app._active_state_tending_conditions(job.id), ((
            JobTriggerType.POWER_ATTENTION_REQUIRED,
            JobReadinessEventType.POWER_RECOVERED,
        ),))
        with self.assertLogs("embodied_runtime.app", level="INFO") as captured:
            outcome = await app.work_current_job_once()

        self.assertEqual(json.loads(backend.results[0].output)["status"], "rejected")
        self.assertIn("power_attention_required", backend.results[0].output)
        self.assertIn("power_recovered", backend.results[0].output)
        self.assertIs(outcome.disposition, JobWorkDisposition.CONTINUE)
        self.assertIs(self.store.get_run(binding.run.id).status, JobRunStatus.RUNNING)
        self.assertIs(app._parked_job_run.binding.task.status, TaskStatus.PAUSED)
        self.assertIs(app.job_continuation.readiness,
                      JobContinuationReadiness.WAIT_FOR_EVENT)
        armed = next(index for index, line in enumerate(captured.output)
                     if "continuation=armed readiness=wait_for_event" in line)
        parked = next(index for index, line in enumerate(captured.output)
                      if "continuation=parked" in line)
        self.assertLess(armed, parked)
        self.assertIn("event=power_recovered", captured.output[armed])

        app._replace_power_state(PowerState(
            7.8, PowerCondition.NORMAL, datetime.now(UTC)))
        app.episode_coordinator._operator_waiters = 1
        await app.events.publish(PowerRecovered(source="test", battery_voltage_v=7.8,
            timestamp_ns=app.job_continuation.event_armed_after_ns + 1))
        await self.drain()
        self.assertTrue(app.job_continuation.event_satisfied)
        self.assertEqual(app.job_continuation.run_id, binding.run.id)
        self.assertEqual(app.job_continuation.task_id, binding.task.id)
        app.episode_coordinator._operator_waiters = 0
        await app.stop()

    async def test_operator_notification_does_not_justify_power_delay(self):
        app = self.app(CorrectingBackend(()))
        await app.start()
        await app._power_monitor.stop()
        job = self.store.create_job("Tend power")
        self.store.set_trigger(job.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
        app._replace_power_state(PowerState(
            7.2, PowerCondition.ATTENTION, datetime.now(UTC)))
        initiative = InitiativeOutcome(
            "connect power", "address_operator", "applied", (),
            (InitiativeEffectOutcome("address_operator", "applied", "delivered"),),
        )
        with self.assertRaisesRegex(ValueError, "passage of time"):
            app._validate_state_tending_outcome(
                job.id, JobWorkDisposition.CONTINUE,
                JobContinuationReadiness.AFTER_DELAY, None, initiative)
        app._validate_state_tending_outcome(
            job.id, JobWorkDisposition.CONTINUE,
            JobContinuationReadiness.WAIT_FOR_EVENT,
            JobReadinessEventType.POWER_RECOVERED, initiative)
        await app.stop()

    async def test_thermal_and_memory_use_current_condition_independently(self):
        app = self.app(CorrectingBackend(()))
        await app.start()
        job = self.store.create_job("Tend health")
        self.store.set_trigger(job.id, JobTriggerType.THERMAL_WARNING_RAISED)
        self.store.set_trigger(job.id, JobTriggerType.MEMORY_PRESSURE_RAISED)
        app._platform_monitor._thermal_warning = False
        app._platform_monitor._memory_pressure = True
        initiative = InitiativeOutcome("checked", None, None)

        with self.assertRaisesRegex(ValueError, "memory_pressure_raised"):
            app._validate_state_tending_outcome(
                job.id, JobWorkDisposition.COMPLETED, None, None, initiative)
        with self.assertRaisesRegex(ValueError, "does not recover"):
            app._validate_state_tending_outcome(
                job.id, JobWorkDisposition.CONTINUE,
                JobContinuationReadiness.WAIT_FOR_EVENT,
                JobReadinessEventType.THERMAL_WARNING_CLEARED, initiative)
        app._platform_monitor._memory_pressure = False
        app._validate_state_tending_outcome(
            job.id, JobWorkDisposition.COMPLETED, None, None, initiative)
        await app.stop()

    async def test_non_state_delay_remains_available_but_unknown_effect_cannot_opt_in(self):
        app = self.app(CorrectingBackend(()))
        await app.start()
        ordinary = self.store.create_job("Ordinary")
        no_effect = InitiativeOutcome("checked", None, None)
        app._validate_state_tending_outcome(
            ordinary.id, JobWorkDisposition.COMPLETED, None, None, no_effect)
        app._validate_state_tending_outcome(
            ordinary.id, JobWorkDisposition.CONTINUE,
            JobContinuationReadiness.AFTER_DELAY, None, no_effect)

        state_job = self.store.create_job("Future control")
        self.store.set_trigger(state_job.id, JobTriggerType.THERMAL_WARNING_RAISED)
        app._platform_monitor._thermal_warning = True
        unknown_effect = InitiativeOutcome(
            "unknown effect", "future_effect", "applied", (),
            (InitiativeEffectOutcome("future_effect", "applied", "ok"),),
        )
        with self.assertRaisesRegex(ValueError, "passage of time"):
            app._validate_state_tending_outcome(
                state_job.id, JobWorkDisposition.CONTINUE,
                JobContinuationReadiness.AFTER_DELAY, None, unknown_effect)
        await app.stop()

    async def test_two_invalid_attempts_park_and_third_is_consumed(self):
        invalid = {"disposition": "completed", "summary": "not recovered",
                   "readiness": None, "delay_seconds": None, "event_type": None}
        backend = CorrectingBackend((invalid, invalid, invalid))
        app = self.app(backend)
        await app.start()
        await app._power_monitor.stop()
        job = self.store.create_job("Tend power")
        self.store.set_trigger(job.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
        backend.before_outcome = lambda: app._replace_power_state(PowerState(
            7.2, PowerCondition.ATTENTION, datetime.now(UTC)))
        binding = app.start_job_run(job.id)

        with self.assertLogs("embodied_runtime.app", level="INFO") as captured:
            outcome = await app.work_current_job_once()

        statuses = [json.loads(result.output)["status"] for result in backend.results]
        self.assertEqual(statuses, ["rejected", "rejected", "rejected"])
        self.assertIn("already consumed", backend.results[2].output)
        self.assertIs(outcome.disposition, JobWorkDisposition.CONTINUE)
        self.assertIs(self.store.get_run(binding.run.id).status, JobRunStatus.RUNNING)
        self.assertIsNone(app._current_job_run)
        self.assertEqual(app._parked_job_run.binding.run.id, binding.run.id)
        self.assertEqual(app._parked_job_run.binding.task.id, binding.task.id)
        self.assertIs(app._parked_job_run.binding.task.status, TaskStatus.PAUSED)
        self.assertIs(app.job_continuation.state,
                      JobContinuationState.AWAITING_OPERATOR)
        self.assertIs(app.job_continuation.readiness,
                      JobContinuationReadiness.WAIT_FOR_OPERATOR)
        self.assertIsNone(app.job_continuation.eligible_at_monotonic)
        self.assertIsNone(app.job_continuation.event_type)
        self.assertTrue(any("continuation=awaiting_operator reason=invalid_outcome" in line
                            for line in captured.output))
        unrelated = app.start_job_run(self.store.create_job("Unrelated").id)
        self.assertEqual(app.current_job_run.run.id, unrelated.run.id)
        await app.stop()

    async def test_after_delay_accepted_path_logs_armed_before_park(self):
        backend = CorrectingBackend((
            {"disposition": "continue", "summary": "settle",
             "readiness": "after_delay", "delay_seconds": 30,
             "event_type": None},
        ))
        app = self.app(backend)
        await app.start()
        app.start_job_run(self.store.create_job("Timed work").id)
        with self.assertLogs("embodied_runtime.app", level="INFO") as captured:
            await app.work_current_job_once()
        armed = next(index for index, line in enumerate(captured.output)
                     if "continuation=armed readiness=after_delay" in line)
        parked = next(index for index, line in enumerate(captured.output)
                      if "continuation=parked" in line)
        self.assertLess(armed, parked)
        self.assertIn("delay_s=30", captured.output[armed])
        await app.stop()

    async def test_automatic_invalid_correction_exhaustion_reparks_occurrence(self):
        wait = {"disposition": "continue", "summary": "waiting",
                "readiness": "wait_for_event", "delay_seconds": None,
                "event_type": "power_recovered"}
        invalid = {"disposition": "continue", "summary": "poll",
                   "readiness": "after_delay", "delay_seconds": 30,
                   "event_type": None}
        backend = GroupedOutcomeBackend(((wait,), (invalid, invalid, invalid)))
        app = self.app(backend)
        await app.start()
        await app._power_monitor.stop()
        job = self.store.create_job("Tend power")
        self.store.set_trigger(job.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
        backend.before_outcome = lambda: app._replace_power_state(PowerState(
            7.2, PowerCondition.ATTENTION, datetime.now(UTC)))
        binding = app.start_job_run(job.id)
        await app.work_current_job_once()

        await app.events.publish(PowerRecovered(
            source="test", battery_voltage_v=7.8,
            timestamp_ns=app.job_continuation.event_armed_after_ns + 1,
        ))
        await self.drain()

        self.assertEqual([json.loads(result.output)["status"]
                          for result in backend.results[-3:]],
                         ["rejected", "rejected", "rejected"])
        self.assertIn("already consumed", backend.results[-1].output)
        self.assertIsNone(app._current_job_run)
        self.assertEqual(app._parked_job_run.binding.run.id, binding.run.id)
        self.assertEqual(app._parked_job_run.binding.task.id, binding.task.id)
        self.assertIs(app._parked_job_run.binding.task.status, TaskStatus.PAUSED)
        self.assertIs(app.job_continuation.state,
                      JobContinuationState.AWAITING_OPERATOR)
        self.assertIsNone(app.job_continuation.eligible_at_monotonic)
        self.assertIsNone(app.job_continuation.event_type)
        unrelated = app.start_job_run(self.store.create_job("Other work").id)
        self.assertEqual(app.current_job_run.run.id, unrelated.run.id)
        await app.stop()

    async def test_automatic_event_continuation_logs_once_before_parking(self):
        wait = {"disposition": "continue", "summary": "waiting",
                "readiness": "wait_for_event", "delay_seconds": None,
                "event_type": "power_recovered"}
        backend = GroupedOutcomeBackend(((wait,), (wait,)))
        app = self.app(backend)
        await app.start()
        await app._power_monitor.stop()
        job = self.store.create_job("Tend power")
        self.store.set_trigger(job.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
        backend.before_outcome = lambda: app._replace_power_state(PowerState(
            7.2, PowerCondition.ATTENTION, datetime.now(UTC)))
        binding = app.start_job_run(job.id)
        await app.work_current_job_once()

        with self.assertLogs("embodied_runtime.app", level="INFO") as captured:
            await app.events.publish(PowerRecovered(
                source="test", battery_voltage_v=7.8,
                timestamp_ns=app.job_continuation.event_armed_after_ns + 1,
            ))
            await self.drain()

        armed = [index for index, line in enumerate(captured.output)
                 if "continuation=armed" in line]
        self.assertEqual(len(armed), 1)
        diagnostic = captured.output[armed[0]]
        self.assertIn(f"job=JOB{binding.job.id}", diagnostic)
        self.assertIn(f"run=RUN{binding.run.id}", diagnostic)
        self.assertIn("readiness=wait_for_event", diagnostic)
        self.assertIn("event=power_recovered", diagnostic)
        self.assertIn("remaining=2", diagnostic)
        self.assertIn("source=heartbeat", diagnostic)
        parked = next(index for index, line in enumerate(captured.output)
                      if "continuation=parked" in line)
        self.assertLess(armed[0], parked)
        await app.stop()
