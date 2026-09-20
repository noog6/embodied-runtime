import asyncio
from dataclasses import replace
import json
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import (
    JOB_OUTCOME_EVALUATION_REQUEST, ApplicationOptions, REPORT_JOB_OUTCOME_TOOL,
    RobotApplication,
)
from embodied_runtime.cognition import CognitionToolCall
from embodied_runtime.console import RuntimeConsole
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    MAX_JOB_CONTINUATION_DELAY_SECONDS, JobContinuationReadiness,
    JobContinuationState, JobRunStatus, SQLiteJobStore,
)
from embodied_runtime.profile import RobotProfile
from tests.test_job_execution import JobBackend, Platform


class ReadinessBackend(JobBackend):
    def __init__(self, proposals):
        super().__init__()
        self.proposals = iter(proposals)
        self.results = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tuple(tool.name for tool in tools)))
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            proposal = next(self.proposals)
            self.results.append(await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name, json.dumps(proposal),
            )))
        return "bounded work"


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class JobReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")
        self.clock = Clock()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def app(self, backend, *, max_steps=3, store=None, wall_clock=None):
        async def sleep_forever(_delay):
            await asyncio.Event().wait()
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True,
                               initiative_goal_closure_enabled=True,
                               jobs_auto_continue=True, jobs_max_auto_steps=max_steps),
            platform_provider=Platform(), cognition_backend=backend,
            job_store=store or self.store, monotonic_clock=self.clock,
            wall_clock=wall_clock or (lambda: datetime(2026, 9, 20, tzinfo=UTC)),
            job_continuation_sleep=sleep_forever,
        )

    async def start_job(self, app):
        await app.start()
        app.start_job_run(self.store.create_job("Inspect").id)

    async def drain(self):
        for _ in range(12):
            await asyncio.sleep(0)

    async def test_after_delay_defers_without_charge_then_becomes_eligible(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "A",
             "readiness": "after_delay", "delay_seconds": 300},
            {"disposition": "continue", "summary": "B",
             "readiness": "ready", "delay_seconds": None},
        ))
        app = self.app(backend)
        await self.start_job(app)
        await app.work_current_job_once()
        continuation = app.job_continuation
        self.assertIs(continuation.readiness, JobContinuationReadiness.AFTER_DELAY)
        self.assertEqual(continuation.eligible_at_monotonic, 400)
        diagnostic = RuntimeConsole(app).execute("job current")[0]
        self.assertIn("readiness:     after_delay", diagnostic)
        self.assertIn("delay_remaining: 300s", diagnostic)
        self.clock.now = 399
        app._offer_job_continuation()
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        self.assertEqual(len(backend.requests), 2)
        self.clock.now = 520
        app._offer_job_continuation()
        await self.drain()
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        self.assertIs(app.job_continuation.readiness, JobContinuationReadiness.READY)
        self.assertIsNone(app.job_continuation.eligible_at_monotonic)
        await app.stop()

    async def test_malformed_after_delay_without_deadline_fails_closed(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "A",
             "readiness": "after_delay", "delay_seconds": 60},
        ))
        app = self.app(backend)
        await self.start_job(app)
        await app.work_current_job_once()
        app._job_continuation = replace(
            app.job_continuation, eligible_at_monotonic=None,
        )
        malformed = app.job_continuation
        app._offer_job_continuation()
        self.assertIsNone(app.job_continuation)
        self.assertEqual(malformed.automatic_steps_remaining, 3)
        self.assertEqual(len(backend.requests), 2)
        self.assertIsNone(app.episode_coordinator.current)
        await app.stop()

    async def test_delay_diagnostic_rounds_up_clamps_and_is_delay_only(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "A",
             "readiness": "after_delay", "delay_seconds": 1},
        ))
        app = self.app(backend)
        await self.start_job(app)
        await app.work_current_job_once()
        self.clock.now = 100.8
        self.assertEqual(app.job_continuation_delay_remaining(), 1)
        self.assertIn("delay_remaining: 1s", RuntimeConsole(app).execute("job current")[0])
        self.clock.now = 102
        self.assertEqual(app.job_continuation_delay_remaining(), 0)
        self.assertIn("delay_remaining: 0s", RuntimeConsole(app).execute("job current")[0])
        app._job_continuation = replace(
            app.job_continuation, readiness=JobContinuationReadiness.READY,
            eligible_at_monotonic=None,
        )
        self.assertIsNone(app.job_continuation_delay_remaining())
        self.assertNotIn("delay_remaining", RuntimeConsole(app).execute("job current")[0])
        await app.stop()

    async def test_expired_delay_still_defers_to_operator_fairness(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "A",
             "readiness": "after_delay", "delay_seconds": 60},
            {"disposition": "continue", "summary": "B",
             "readiness": "ready", "delay_seconds": None},
        ))
        app = self.app(backend)
        await self.start_job(app)
        await app.work_current_job_once()
        self.clock.now = 161
        app.episode_coordinator._operator_waiters = 1
        app._offer_job_continuation()
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        self.assertIsNone(app._active_job_work_task)
        app.episode_coordinator._operator_waiters = 0
        app._offer_job_continuation()
        await self.drain()
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        await app.stop()

    async def test_manual_work_overrides_after_delay_with_fresh_burst(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "summary A",
             "readiness": "after_delay", "delay_seconds": 600},
            {"disposition": "continue", "summary": "summary B",
             "readiness": "ready", "delay_seconds": None},
        ))
        app = self.app(backend)
        await self.start_job(app)
        await app.work_current_job_once()
        old_deadline = app.job_continuation.eligible_at_monotonic
        await app.work_current_job_once()
        self.assertIn("summary A", backend.requests[2][1])
        self.assertIs(app.job_continuation.readiness, JobContinuationReadiness.READY)
        self.assertIsNone(app.job_continuation.eligible_at_monotonic)
        self.assertNotEqual(app.job_continuation.eligible_at_monotonic, old_deadline)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        await app.stop()

    async def test_budget_survives_ready_delay_ready_transitions(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "A",
             "readiness": "ready", "delay_seconds": None},
            {"disposition": "continue", "summary": "B",
             "readiness": "after_delay", "delay_seconds": 60},
            {"disposition": "continue", "summary": "C",
             "readiness": "ready", "delay_seconds": None},
        ))
        app = self.app(backend)
        await self.start_job(app)
        await app.work_current_job_once()
        app._offer_job_continuation()
        await self.drain()
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        self.clock.now = 159
        app._offer_job_continuation()
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        self.clock.now = 160
        app._offer_job_continuation()
        await self.drain()
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 1)
        await app.stop()

    async def test_budget_exhaustion_preserves_latest_readiness_and_summary(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "initial",
             "readiness": "ready", "delay_seconds": None},
            {"disposition": "continue", "summary": "latest",
             "readiness": "after_delay", "delay_seconds": 300},
        ))
        app = self.app(backend, max_steps=1)
        await self.start_job(app)
        await app.work_current_job_once()
        app._offer_job_continuation()
        await self.drain()
        continuation = app.job_continuation
        self.assertIs(continuation.state, JobContinuationState.AWAITING_OPERATOR)
        self.assertIs(continuation.readiness, JobContinuationReadiness.AFTER_DELAY)
        self.assertEqual(continuation.last_summary, "latest")
        self.assertEqual(continuation.eligible_at_monotonic, 400)
        await app.stop()

    async def test_manual_work_overrides_operator_wait_with_continuity(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "summary A",
             "readiness": "wait_for_operator", "delay_seconds": None},
            {"disposition": "continue", "summary": "summary B",
             "readiness": "ready", "delay_seconds": None},
        ))
        app = self.app(backend)
        await self.start_job(app)
        await app.work_current_job_once()
        self.assertIn("readiness:     wait_for_operator",
                      RuntimeConsole(app).execute("job current")[0])
        self.assertNotIn("delay_remaining",
                         RuntimeConsole(app).execute("job current")[0])
        for _ in range(5):
            app._offer_job_continuation()
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        await app.work_current_job_once()
        self.assertIn("summary A", backend.requests[2][1])
        self.assertIs(app.job_continuation.readiness, JobContinuationReadiness.READY)
        await app.stop()

    async def test_pause_resume_retains_expired_delay_and_continuity(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "summary A",
             "readiness": "after_delay", "delay_seconds": 60},
            {"disposition": "continue", "summary": "summary B",
             "readiness": "ready", "delay_seconds": None},
        ))
        app = self.app(backend)
        await self.start_job(app)
        await app.work_current_job_once()
        old_goal = app.active_goal
        app.pause_task()
        self.clock.now = 200
        app._offer_job_continuation()
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        self.assertEqual(app.job_continuation.last_summary, "summary A")
        app.resume_task()
        self.assertIsNot(app.active_goal, old_goal)
        self.assertEqual(len(backend.requests), 2)
        app._offer_job_continuation()
        await self.drain()
        self.assertIn("summary A", backend.requests[2][1])
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        await app.stop()

    async def test_unrelated_operator_interaction_does_not_resume_waiting_job(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "summary A",
             "readiness": "wait_for_operator", "delay_seconds": None},
            {"disposition": "continue", "summary": "summary B",
             "readiness": "ready", "delay_seconds": None},
        ))
        app = self.app(backend)
        await self.start_job(app)
        await app.work_current_job_once()
        await app.handle_operator_utterance("hello", source="console")
        self.assertIs(app.job_continuation.readiness,
                      JobContinuationReadiness.WAIT_FOR_OPERATOR)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        self.assertIsNone(app._active_job_work_task)
        await app.work_current_job_once()
        self.assertIn("summary A", backend.requests[-2][1])
        await app.stop()

    async def test_terminal_paths_clear_each_readiness(self):
        proposals = tuple(
            {"disposition": "continue", "summary": readiness,
             "readiness": readiness,
             "delay_seconds": 60 if readiness == "after_delay" else None}
            for readiness in ("ready", "after_delay", "wait_for_operator")
        )
        backend = ReadinessBackend(proposals)
        app = self.app(backend)
        await app.start()
        job = self.store.create_job("Terminal")
        for readiness, status in zip(
            ("ready", "after_delay", "wait_for_operator"),
            (JobRunStatus.COMPLETED, JobRunStatus.FAILED, JobRunStatus.STOPPED),
        ):
            with self.subTest(readiness=readiness, status=status.value):
                app.start_job_run(job.id)
                await app.work_current_job_once()
                self.assertIsNotNone(app.job_continuation)
                request_count = len(backend.requests)
                app.finish_job_run(status, "terminal")
                self.assertIsNone(app.job_continuation)
                app._offer_job_continuation()
                await self.drain()
                self.assertEqual(len(backend.requests), request_count)
        await app.stop()

    async def test_new_run_does_not_inherit_readiness_or_continuity(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "summary A",
             "readiness": "after_delay", "delay_seconds": 60},
        ))
        app = self.app(backend)
        await app.start()
        job = self.store.create_job("Repeat")
        first = app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertIsNotNone(app.job_continuation.eligible_at_monotonic)
        app.finish_job_run(JobRunStatus.STOPPED)
        second = app.start_job_run(job.id)
        self.assertNotEqual(first.run.id, second.run.id)
        self.assertIsNone(app.job_continuation)
        self.assertNotIn("Previous bounded Job work", app.active_goal.description)
        await app.stop()

    async def test_shutdown_restart_discards_readiness(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "summary A",
             "readiness": "after_delay", "delay_seconds": 600},
        ))
        app = self.app(backend)
        await self.start_job(app)
        job_id = app.current_job_run.job.id
        await app.work_current_job_once()
        await app.stop()
        reopened = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")
        self.store = reopened
        fresh = self.app(JobBackend(), store=reopened)
        await fresh.start()
        self.assertIsNone(fresh.current_job_run)
        self.assertIsNone(fresh.current_task)
        self.assertIsNone(fresh.job_continuation)
        self.assertIs(reopened.list_runs(job_id)[0].status, JobRunStatus.RUNNING)
        await fresh.stop()

    async def test_scheduled_after_delay_uses_ordinary_continuation_path(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "summary A",
             "readiness": "after_delay", "delay_seconds": 120},
            {"disposition": "continue", "summary": "summary B",
             "readiness": "ready", "delay_seconds": None},
        ))
        due = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
        app = self.app(backend, wall_clock=lambda: due)
        job = self.store.create_job("Scheduled delay")
        self.store.set_schedule(job.id, "02:00", "UTC")
        await app.start()
        await app._offer_scheduled_job()
        await app._active_job_work_task
        continuation = app.job_continuation
        self.assertIs(app.current_job_run.run.status, JobRunStatus.RUNNING)
        self.assertIs(continuation.readiness, JobContinuationReadiness.AFTER_DELAY)
        self.assertEqual(continuation.eligible_at_monotonic, 220)
        self.assertEqual(continuation.automatic_steps_remaining, 3)
        self.assertEqual(continuation.last_summary, "summary A")
        self.clock.now = 219
        app._offer_job_continuation()
        self.assertEqual(len(backend.requests), 2)
        self.clock.now = 220
        app._offer_job_continuation()
        await self.drain()
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        await app.stop()

    async def test_scheduled_operator_wait_resumes_only_manually(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "summary A",
             "readiness": "wait_for_operator", "delay_seconds": None},
            {"disposition": "continue", "summary": "summary B",
             "readiness": "ready", "delay_seconds": None},
        ))
        due = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
        app = self.app(backend, wall_clock=lambda: due)
        job = self.store.create_job("Scheduled operator")
        self.store.set_schedule(job.id, "02:00", "UTC")
        await app.start()
        await app._offer_scheduled_job()
        await app._active_job_work_task
        for _ in range(3):
            app._offer_job_continuation()
        self.assertIs(app.current_job_run.run.status, JobRunStatus.RUNNING)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        self.assertEqual(len(backend.requests), 2)
        await app.work_current_job_once()
        self.assertIn("summary A", backend.requests[2][1])
        self.assertIs(app.job_continuation.readiness, JobContinuationReadiness.READY)
        await app.stop()

    async def test_invalid_cross_field_combinations_do_not_arm(self):
        invalid = (
            ("completed", "ready", None), ("failed", "wait_for_operator", None),
            ("continue", None, None), ("continue", "ready", 10),
            ("continue", "wait_for_operator", 10),
            ("continue", "after_delay", None), ("continue", "after_delay", 0),
            ("continue", "after_delay", -1),
            ("continue", "after_delay", MAX_JOB_CONTINUATION_DELAY_SECONDS + 1),
            ("continue", "after_delay", True), ("continue", "unknown", None),
        )
        backend = ReadinessBackend(tuple({
            "disposition": disposition, "summary": "result",
            "readiness": readiness, "delay_seconds": delay,
        } for disposition, readiness, delay in invalid))
        app = self.app(backend)
        await self.start_job(app)
        for index, (disposition, readiness, delay) in enumerate(invalid):
            with self.subTest(disposition=disposition, readiness=readiness, delay=delay):
                outcome = await app.work_current_job_once()
                self.assertEqual(json.loads(backend.results[index].output)["status"], "rejected")
                self.assertIsNone(app.job_continuation)
                self.assertEqual(outcome.disposition.value, "continue")
        await app.stop()
