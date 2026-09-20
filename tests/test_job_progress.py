import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from embodied_runtime.app import (
    JOB_OUTCOME_EVALUATION_REQUEST, REPORT_JOB_OUTCOME_TOOL,
    ApplicationOptions, RobotApplication,
)
from embodied_runtime.console import RuntimeConsole
from embodied_runtime.cognition import (
    CognitionToolCall, InitiativeAcquisitionOutcome, InitiativeEffectOutcome,
    TextCognitionBackend,
)
from embodied_runtime.attention import InitiativeOutcome
from embodied_runtime.events import PresenceChanged
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    MAX_JOB_PROGRESS_COUNTERS, MAX_JOB_PROGRESS_COUNTER_VALUE,
    JobProgress, JobProgressCounter, JobProgressUpdate, JobRunStatus,
    JobReadinessEventType, JobWakeEvent, JobWorkDisposition, SQLiteJobStore,
    JobContinuationState, validate_counter_name,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.tasks import TaskStatus
from tests.test_job_execution import Platform


class ProgressBackend(TextCognitionBackend):
    identifier = "progress-test"

    def __init__(self, proposals, *, fail_after=None, after_tool=None):
        self.proposals = iter(proposals)
        self.fail_after = fail_after
        self.after_tool = after_tool
        self.outcomes = 0
        self.requests = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tuple(tool.name for tool in tools)))
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            proposal = next(self.proposals)
            await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name, json.dumps(proposal),
            ))
            self.outcomes += 1
            if self.after_tool is not None:
                self.after_tool()
            if self.fail_after == self.outcomes:
                raise RuntimeError("provider failed")
        return "bounded work"


def proposal(*, progress=None, disposition="continue", readiness="wait_for_event"):
    return {
        "disposition": disposition,
        "summary": "bounded result",
        "readiness": readiness if disposition == "continue" else None,
        "delay_seconds": None,
        "event_type": (
            "presence_changed"
            if disposition == "continue" and readiness == "wait_for_event" else None
        ),
        "progress_update": progress,
    }


class JobProgressModelTests(unittest.TestCase):
    def test_counter_grammar_and_harness_owned_increment(self):
        for name in ("presence_changes_seen", "log_chunks_reviewed"):
            self.assertEqual(validate_counter_name(name), name)
        for name in ("", "Presence Changes", "hello!", "_foo", "a" * 49):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_counter_name(name)
        progress = JobProgress(1, 2, uuid4())
        update = JobProgressUpdate("checks_completed", "acquisition_1")
        self.assertEqual(progress.increment(update).counters[0].value, 1)
        self.assertEqual(progress.increment(update).increment(update).counters[0].value, 2)

    def test_counter_catalog_and_value_bounds_leave_snapshot_immutable(self):
        task_id = uuid4()
        full = JobProgress(1, 2, task_id, tuple(
            JobProgressCounter(f"counter_{index}", 1)
            for index in range(MAX_JOB_PROGRESS_COUNTERS)
        ))
        with self.assertRaises(ValueError):
            full.increment(JobProgressUpdate("another", "effect_1"))
        maximum = JobProgress(
            1, 2, task_id,
            (JobProgressCounter("checks", MAX_JOB_PROGRESS_COUNTER_VALUE),),
        )
        with self.assertRaises(ValueError):
            maximum.increment(JobProgressUpdate("checks", "effect_1"))
        self.assertEqual(maximum.counters[0].value, MAX_JOB_PROGRESS_COUNTER_VALUE)


class JobProgressRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def app(self, backend, *, max_steps=3, wall_clock=None):
        async def sleep_forever(_delay):
            await asyncio.Event().wait()
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(
                initiative_enabled=True, jobs_auto_continue=True,
                jobs_max_auto_steps=max_steps,
            ),
            platform_provider=Platform(), cognition_backend=backend,
            job_store=self.store,
            wall_clock=wall_clock or (lambda: datetime(2026, 9, 20, tzinfo=UTC)),
            job_continuation_sleep=sleep_forever,
        )

    async def drain(self):
        for _ in range(15):
            await asyncio.sleep(0)

    @staticmethod
    def set_progress(app, *counters):
        binding = app.current_job_run
        app._job_progress = JobProgress(
            binding.job.id, binding.run.id, binding.task.id,
            tuple(JobProgressCounter(name, value) for name, value in counters),
        )

    async def test_two_knock_commits_first_wake_and_completes_on_second(self):
        backend = ProgressBackend((
            proposal(),
            proposal(progress={"counter": "presence_changes_seen",
                               "basis": "wake_event"}),
            proposal(disposition="completed"),
        ))
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(self.store.create_job("Two knocks").id)
        self.assertEqual(app.job_progress.counters, ())
        await app.work_current_job_once()

        await app.events.publish(PresenceChanged(
            source="test", previous_present=False, present=True,
            timestamp_ns=app.job_continuation.event_armed_after_ns + 1,
        ))
        await self.drain()
        app._offer_job_continuation()
        await self.drain()
        self.assertEqual(
            app.job_progress.counters,
            (JobProgressCounter("presence_changes_seen", 1),),
        )
        request_count = len(backend.requests)
        app._offer_job_continuation()
        await self.drain()
        self.assertEqual(len(backend.requests), request_count)

        await app.events.publish(PresenceChanged(
            source="test", previous_present=True, present=False,
            timestamp_ns=app.job_continuation.event_armed_after_ns + 1,
        ))
        await self.drain()
        app._offer_job_continuation()
        await self.drain()
        self.assertIn("Current Job progress", backend.requests[-2][1])
        self.assertIn("presence_changes_seen: 1", backend.requests[-2][1])
        self.assertIsNone(app.current_job_run)
        self.assertIsNone(app.job_progress)
        self.assertIs(self.store.get_run(binding.run.id).status, JobRunStatus.COMPLETED)
        self.assertEqual(backend.outcomes, 3)
        await app.stop()

    async def test_wake_basis_without_wake_is_rejected(self):
        backend = ProgressBackend((proposal(progress={
            "counter": "presence_changes_seen", "basis": "wake_event",
        }),))
        app = self.app(backend)
        await app.start()
        app.start_job_run(self.store.create_job("No fake wake").id)
        outcome = await app.work_current_job_once()
        self.assertIs(outcome.disposition, JobWorkDisposition.CONTINUE)
        self.assertIsNone(outcome.summary)
        self.assertEqual(app.job_progress.counters, ())
        await app.stop()

    async def test_provider_failure_after_staging_does_not_commit(self):
        backend = ProgressBackend((proposal(progress={
            "counter": "seen", "basis": "wake_event",
        }),), fail_after=1)
        app = self.app(backend)
        await app.start()
        app.start_job_run(self.store.create_job("Failure").id)
        prepared = app._validate_job_work_preconditions()
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            await app._work_current_job_once(
                "test", prepared=prepared,
                wake_event=JobWakeEvent(JobReadinessEventType.PRESENCE_CHANGED, True),
            )
        self.assertEqual(app.job_progress.counters, ())
        await app.stop()

    async def test_stale_staged_update_clears_only_its_occurrence(self):
        app = None

        def invalidate_run_one():
            app.finish_task(TaskStatus.STOPPED)

        backend = ProgressBackend((proposal(
            progress={"counter": "checks_completed", "basis": "wake_event"},
            readiness="ready",
        ),), after_tool=invalidate_run_one)
        app = self.app(backend)
        await app.start()
        stale = app.start_job_run(self.store.create_job("Stale").id)
        self.set_progress(app, ("checks_completed", 1))
        prepared = app._validate_job_work_preconditions()
        outcome = await app._work_current_job_once(
            "test", prepared=prepared,
            wake_event=JobWakeEvent(JobReadinessEventType.PRESENCE_CHANGED, True),
        )
        self.assertIs(outcome.disposition, JobWorkDisposition.CONTINUE)
        self.assertIsNone(outcome.summary)
        self.assertIsNone(app._job_progress)
        app.finish_job_run(JobRunStatus.STOPPED)

        replacement = app.start_job_run(stale.job.id)
        self.assertNotEqual(replacement.run.id, stale.run.id)
        self.assertEqual(app.job_progress.counters, ())
        app._clear_stale_job_progress(stale)
        self.assertEqual(app.job_progress.counters, ())
        await app.stop()

    async def test_pause_resume_preserves_progress_and_budget(self):
        backend = ProgressBackend((
            proposal(readiness="ready"), proposal(readiness="ready"),
        ))
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(self.store.create_job("Pause").id)
        await app.work_current_job_once()
        self.set_progress(app, ("checks_completed", 1))
        remaining = app.job_continuation.automatic_steps_remaining
        old_goal = app.active_goal

        app.pause_task()
        self.assertEqual(app.job_progress.counters[0].value, 1)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, remaining)
        app.resume_task()
        self.assertEqual(app.current_job_run.run.id, binding.run.id)
        self.assertEqual(app.current_job_run.task.id, binding.task.id)
        self.assertIsNot(app.active_goal, old_goal)
        self.assertEqual(app.job_progress.counters[0].value, 1)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, remaining)

        await app.work_current_job_once()
        self.assertIn("checks_completed: 1", backend.requests[-2][1])
        await app.stop()

    async def test_in_flight_pause_rejects_increment_but_preserves_progress(self):
        app = None

        def pause_after_staging():
            app.pause_task()

        backend = ProgressBackend((proposal(
            progress={"counter": "checks_completed", "basis": "wake_event"},
            readiness="ready",
        ),), after_tool=pause_after_staging)
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(self.store.create_job("Pause in flight").id)
        self.set_progress(app, ("checks_completed", 1))
        prepared = app._validate_job_work_preconditions()

        outcome = await app._work_current_job_once(
            "test", prepared=prepared,
            wake_event=JobWakeEvent(JobReadinessEventType.PRESENCE_CHANGED, True),
        )

        self.assertIs(outcome.disposition, JobWorkDisposition.CONTINUE)
        self.assertIsNone(outcome.summary)
        self.assertIs(app.current_task.status, TaskStatus.PAUSED)
        self.assertEqual(app.current_job_run.run.id, binding.run.id)
        self.assertEqual(app.current_job_run.task.id, binding.task.id)
        self.assertEqual(app.job_progress.counters,
                         (JobProgressCounter("checks_completed", 1),))
        app.resume_task()
        self.assertEqual(app.job_progress.counters,
                         (JobProgressCounter("checks_completed", 1),))
        await app.stop()

    async def test_budget_exhaustion_retains_progress_without_granting_work(self):
        backend = ProgressBackend((
            proposal(),
            proposal(progress={"counter": "checks_completed", "basis": "wake_event"},
                     readiness="ready"),
            proposal(readiness="ready"),
        ))
        app = self.app(backend, max_steps=1)
        await app.start()
        app.start_job_run(self.store.create_job("Budget").id)
        await app.work_current_job_once()
        self.set_progress(app, ("checks_completed", 1))
        await app.events.publish(PresenceChanged(
            source="test", previous_present=False, present=True,
            timestamp_ns=app.job_continuation.event_armed_after_ns + 1,
        ))
        await self.drain()
        app._offer_job_continuation()
        await self.drain()
        self.assertEqual(app.job_progress.counters[0].value, 2)
        self.assertIs(app.job_continuation.state,
                      JobContinuationState.AWAITING_OPERATOR)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 0)
        count = len(backend.requests)
        app._offer_job_continuation()
        await self.drain()
        self.assertEqual(len(backend.requests), count)

        await app.work_current_job_once()
        self.assertIn("checks_completed: 2", backend.requests[-2][1])
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 1)
        await app.stop()

    async def test_scheduled_occurrence_starts_empty_and_retains_progress(self):
        clock = lambda: datetime(2026, 9, 21, 3, tzinfo=UTC)
        backend = ProgressBackend((
            proposal(),
            proposal(progress={"counter": "scheduled_steps", "basis": "wake_event"},
                     readiness="ready"),
        ))
        job = self.store.create_job("Scheduled")
        self.store.set_schedule(job.id, "02:00", "UTC")
        app = self.app(backend, wall_clock=clock)
        await app.start()
        await app._offer_scheduled_job()
        await app._active_job_work_task
        binding = app.current_job_run
        self.assertIn("Current Job progress", backend.requests[0][1])
        self.assertIn("  none", backend.requests[0][1])
        marker = self.store.get_schedule(job.id).last_started_local_date

        await app.events.publish(PresenceChanged(
            source="test", previous_present=False, present=True,
            timestamp_ns=app.job_continuation.event_armed_after_ns + 1,
        ))
        await self.drain()
        app._offer_job_continuation()
        await self.drain()
        self.assertEqual(app.current_job_run.run.id, binding.run.id)
        self.assertEqual(app.current_job_run.task.id, binding.task.id)
        self.assertEqual(app.job_progress.counters,
                         (JobProgressCounter("scheduled_steps", 1),))
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.assertEqual(self.store.get_schedule(job.id).last_started_local_date,
                         marker)
        await app.stop()

    async def test_operator_cognition_cannot_mutate_or_access_progress_tool(self):
        backend = ProgressBackend(())
        app = self.app(backend)
        await app.start()
        app.start_job_run(self.store.create_job("Discuss").id)
        self.set_progress(app, ("presence_changes_seen", 1))
        await app.handle_operator_utterance("How is that Job going?")
        self.assertEqual(app.job_progress.counters,
                         (JobProgressCounter("presence_changes_seen", 1),))
        operator_request = backend.requests[-1]
        self.assertNotEqual(operator_request[0], JOB_OUTCOME_EVALUATION_REQUEST)
        self.assertNotIn(REPORT_JOB_OUTCOME_TOOL.name, operator_request[2])
        await app.stop()

    async def test_terminal_cleanup_and_new_run_isolation(self):
        backend = ProgressBackend(())
        app = self.app(backend)
        await app.start()
        job = self.store.create_job("Lifecycle")
        first = app.start_job_run(job.id)
        self.set_progress(app, ("checks_completed", 1))
        app.finish_job_run(JobRunStatus.COMPLETED)
        self.assertIsNone(app.job_progress)
        second = app.start_job_run(job.id)
        self.assertNotEqual(second.run.id, first.run.id)
        self.assertEqual(app.job_progress.counters, ())
        self.set_progress(app, ("checks_completed", 1))
        app.finish_job_run(JobRunStatus.STOPPED)
        self.assertIsNone(app.job_progress)
        await app.stop()

    async def test_shutdown_and_restart_do_not_reconstruct_progress(self):
        backend = ProgressBackend(())
        app = self.app(backend)
        await app.start()
        app.start_job_run(self.store.create_job("Volatile").id)
        self.set_progress(app, ("checks_completed", 1))
        await app.stop()
        self.assertIsNone(app._job_progress)
        self.assertIsNone(app.current_job_run)

        restarted = self.app(ProgressBackend(()))
        await restarted.start()
        self.assertIsNone(restarted.job_progress)
        self.assertIsNone(restarted.current_job_run)
        await restarted.stop()

    async def test_job_current_progress_is_exact_bound_and_sorted(self):
        app = self.app(ProgressBackend(()))
        await app.start()
        binding = app.start_job_run(self.store.create_job("Diagnostic").id)
        console = RuntimeConsole(app)
        self.assertIn("progress:      none", console.execute("job current")[0])
        progress = app.job_progress.increment(JobProgressUpdate("z_step", "wake_event"))
        progress = progress.increment(JobProgressUpdate("z_step", "wake_event"))
        app._job_progress = progress.increment(JobProgressUpdate("a_step", "wake_event"))
        output = console.execute("job current")[0]
        self.assertIn("progress:      a_step=1, z_step=2", output)
        app._job_progress = JobProgress(
            binding.job.id, binding.run.id + 1, binding.task.id,
            (JobProgressCounter("hidden", 1),),
        )
        self.assertIn("progress:      none", console.execute("job current")[0])
        await app.stop()

    def test_deterministic_acquisition_and_effect_basis_validation(self):
        initiative = InitiativeOutcome(
            "response", None, None,
            (InitiativeAcquisitionOutcome("inspect_self", "applied", "ok"),),
            (InitiativeEffectOutcome("orient_body", "rejected", "no"),),
        )
        self.assertEqual(
            RobotApplication._job_progress_bases(initiative, None),
            ("acquisition_1",),
        )
