import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    JobContinuationState, JobRunStatus, SQLiteJobStore,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.tasks import TaskStatus
from tests.test_job_execution import FakeTimer, JobBackend, Platform


class SequencedBackend(JobBackend):
    def __init__(self, dispositions):
        super().__init__()
        self.dispositions = iter(dispositions)

    async def respond(self, message, **kwargs):
        if message.endswith("request work."):
            self.disposition = next(self.dispositions)
        return await super().respond(message, **kwargs)


class FailAutomaticBackend(JobBackend):
    async def respond(self, message, **kwargs):
        if len(self.requests) == 2:
            raise RuntimeError("automatic provider failure")
        return await super().respond(message, **kwargs)


class BlockAutomaticBackend(JobBackend):
    def __init__(self):
        super().__init__()
        self.automatic_started = asyncio.Event()

    async def respond(self, message, **kwargs):
        if len(self.requests) == 2:
            self.automatic_started.set()
            await asyncio.Event().wait()
        return await super().respond(message, **kwargs)


class JobContinuationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.temp.cleanup()

    def app(self, backend, *, timer=None, max_steps=3, enabled=True, **kwargs):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(
                initiative_enabled=True,
                initiative_goal_closure_enabled=True,
                jobs_auto_continue=enabled,
                jobs_heartbeat_seconds=30.0,
                jobs_max_auto_steps=max_steps,
            ),
            platform_provider=Platform(), cognition_backend=backend,
            job_store=self.store,
            wall_clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
            job_continuation_sleep=None if timer is None else timer.sleep,
            **kwargs,
        )

    async def _tick(self, timer):
        await timer.advance()
        for _ in range(12):
            await asyncio.sleep(0)

    async def _arm(self, app, name="Continue"):
        job = self.store.create_job(name)
        binding = app.start_job_run(job.id)
        await app.work_current_job_once()
        return binding

    async def test_manual_continue_arms_without_immediate_recursion(self):
        timer, backend = FakeTimer(), JobBackend()
        job = self.store.create_job("Continue")
        app = self.app(backend, timer=timer)
        await app.start()
        binding = app.start_job_run(job.id)
        await app.work_current_job_once()
        continuation = app.job_continuation
        self.assertEqual(continuation.state, JobContinuationState.ARMED)
        self.assertEqual(continuation.automatic_steps_remaining, 3)
        self.assertEqual((continuation.job_id, continuation.run_id,
                          continuation.task_id),
                         (job.id, binding.run.id, binding.task.id))
        await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 2)
        await app.stop()

    async def test_heartbeats_continue_then_complete_once_each(self):
        timer = FakeTimer()
        backend = SequencedBackend(("continue", "continue", "completed"))
        job = self.store.create_job("Finish later")
        app = self.app(backend, timer=timer)
        await app.start()
        binding = app.start_job_run(job.id)
        await app.work_current_job_once()
        await self._tick(timer)
        self.assertEqual(len(backend.requests), 4)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        await self._tick(timer)
        self.assertEqual(len(backend.requests), 6)
        self.assertIsNone(app.job_continuation)
        self.assertIs(self.store.get_run(binding.run.id).status,
                      JobRunStatus.COMPLETED)
        self.assertIsNone(app.current_task)
        metrics = app.observability.snapshot()["metrics"]
        self.assertEqual(metrics["manual_job_work"], 1)
        self.assertEqual(metrics["automatic_job_work"], 2)
        self.assertEqual(metrics["continuations_accepted"], 2)
        await self._tick(timer)
        self.assertEqual(len(backend.requests), 6)
        await app.stop()

    async def test_budget_exhaustion_and_manual_regrant(self):
        timer, backend = FakeTimer(), JobBackend()
        job = self.store.create_job("Keep going")
        app = self.app(backend, timer=timer, max_steps=2)
        await app.start()
        app.start_job_run(job.id)
        await app.work_current_job_once()
        await self._tick(timer)
        await self._tick(timer)
        self.assertEqual(app.job_continuation.state,
                         JobContinuationState.AWAITING_OPERATOR)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 0)
        calls = len(backend.requests)
        await self._tick(timer)
        self.assertEqual(len(backend.requests), calls)
        await app.work_current_job_once()
        self.assertEqual(app.job_continuation.state, JobContinuationState.ARMED)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        await app.stop()

    async def test_pause_defers_and_resume_uses_fresh_goal(self):
        timer, backend = FakeTimer(), JobBackend()
        job = self.store.create_job("Pause")
        app = self.app(backend, timer=timer)
        await app.start()
        app.start_job_run(job.id)
        await app.work_current_job_once()
        old_goal = app.active_goal
        app.pause_task()
        await self._tick(timer)
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        app.resume_task()
        self.assertIsNot(app.active_goal, old_goal)
        await self._tick(timer)
        self.assertEqual(len(backend.requests), 4)
        await app.stop()

    async def test_disabled_mode_never_creates_controller_or_state(self):
        backend = JobBackend()
        job = self.store.create_job("Manual")
        app = self.app(backend, enabled=False)
        self.assertIsNone(app.job_continuation_controller)
        await app.start()
        app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertIsNone(app.job_continuation)
        await app.stop()

    async def test_operator_waiting_defers_without_consuming_budget(self):
        app = self.app(JobBackend())
        await app.start()
        await self._arm(app)
        app.episode_coordinator._operator_waiters = 1
        app._offer_job_continuation()
        self.assertEqual(app.job_continuation.state, JobContinuationState.ARMED)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        self.assertEqual(len(app._cognition_backend.requests), 2)
        app.episode_coordinator._operator_waiters = 0
        await app.stop()

    async def test_attention_busy_defers_without_consuming_budget(self):
        app = self.app(JobBackend())
        await app.start()
        await self._arm(app)
        busy = app.episode_coordinator.try_start("test", "test", "busy", None)
        self.assertIsNotNone(busy)
        app._offer_job_continuation()
        self.assertEqual(app.job_continuation.state, JobContinuationState.ARMED)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        self.assertEqual(len(app._cognition_backend.requests), 2)
        app.episode_coordinator.close(busy, "handled")
        await app.stop()

    async def test_post_check_attention_claim_race_defers_without_charge(self):
        app = self.app(JobBackend())
        await app.start()
        await self._arm(app)
        original_try_start = app.episode_coordinator.try_start

        def attention_contender(*unused_args):
            original_try_start("test", "contender", "won race", None)
            return None

        with patch.object(
            app.episode_coordinator, "try_start", side_effect=attention_contender,
        ):
            app._offer_job_continuation()
        self.assertEqual(app.job_continuation.state, JobContinuationState.ARMED)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        self.assertIsNone(app._active_job_work_task)
        self.assertEqual(len(app._cognition_backend.requests), 2)
        app.episode_coordinator.close(app.episode_coordinator.current, "handled")
        await app.stop()

    async def test_started_automatic_provider_failure_consumes_step_and_stops(self):
        backend = FailAutomaticBackend()
        app = self.app(backend)
        await app.start()
        binding = await self._arm(app)
        app._offer_job_continuation()
        for _ in range(12):
            await asyncio.sleep(0)
        self.assertEqual(app.job_continuation.state,
                         JobContinuationState.AWAITING_OPERATOR)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        self.assertIs(self.store.get_run(binding.run.id).status,
                      JobRunStatus.RUNNING)
        self.assertIs(app.current_task.status, TaskStatus.RUNNING)
        calls = len(backend.requests)
        app._offer_job_continuation()
        await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), calls)
        await app.stop()

    async def test_shutdown_cancels_automatic_work_and_leaves_run_running(self):
        backend = BlockAutomaticBackend()
        app = self.app(backend)
        await app.start()
        binding = await self._arm(app)
        app._offer_job_continuation()
        await backend.automatic_started.wait()
        await app.stop()
        self.assertIsNone(app.job_continuation)
        self.assertIsNone(app.current_job_run)
        reopened = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")
        self.addCleanup(reopened.close)
        self.assertIs(reopened.get_run(binding.run.id).status, JobRunStatus.RUNNING)

    async def test_restart_does_not_adopt_durable_running_run(self):
        timer, backend = FakeTimer(), JobBackend()
        app = self.app(backend, timer=timer)
        await app.start()
        binding = await self._arm(app)
        await app.stop()
        reopened = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")
        self.store = reopened
        fresh_timer, fresh_backend = FakeTimer(), JobBackend()
        fresh = self.app(fresh_backend, timer=fresh_timer)
        await fresh.start()
        self.assertIs(reopened.get_run(binding.run.id).status, JobRunStatus.RUNNING)
        self.assertIsNone(fresh.current_job_run)
        self.assertIsNone(fresh.current_task)
        self.assertIsNone(fresh.job_continuation)
        await self._tick(fresh_timer)
        await self._tick(fresh_timer)
        self.assertEqual(fresh_backend.requests, [])
        await fresh.stop()

    async def test_manual_work_supersedes_armed_grant(self):
        app = self.app(JobBackend())
        await app.start()
        await self._arm(app)
        app._job_continuation = replace(
            app.job_continuation, automatic_steps_remaining=1,
        )
        await app.work_current_job_once()
        self.assertEqual(app.job_continuation.state, JobContinuationState.ARMED)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        self.assertEqual(len(app._cognition_backend.requests), 4)
        await app.stop()

    async def test_terminal_operations_clear_continuation(self):
        self.store.close()
        for status in (
            JobRunStatus.COMPLETED, JobRunStatus.FAILED, JobRunStatus.STOPPED,
        ):
            with self.subTest(status=status):
                self.store = SQLiteJobStore(
                    Path(self.temp.name) / f"{status.value}.sqlite3"
                )
                app = self.app(JobBackend())
                await app.start()
                await self._arm(app, status.value)
                app.finish_job_run(status, "terminal")
                self.assertIsNone(app.job_continuation)
                self.assertIsNone(app.current_job_run)
                await app.stop()
