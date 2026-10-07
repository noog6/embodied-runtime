import asyncio
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    JobContinuationState, JobProgress, JobProgressCounter, JobRunStatus, SQLiteJobStore,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.state import LifecycleState
from embodied_runtime.tasks import TaskStatus
from tests.test_job_execution import Platform
from tests.test_job_readiness import Clock, ReadinessBackend


DELAYED = {"disposition": "continue", "summary": "parked progress",
           "readiness": "after_delay", "delay_seconds": 60}
COMPLETED = {"disposition": "completed", "summary": "finished",
             "readiness": None, "delay_seconds": None}
READY = {"disposition": "continue", "summary": "more work",
         "readiness": "ready", "delay_seconds": None}


class ControlledHeartbeat:
    """A heartbeat advances only when the test releases its current wait."""

    def __init__(self):
        self.waits = asyncio.Queue()
        self.delays = []
        self.gate = None

    async def sleep(self, delay):
        self.delays.append(delay)
        gate = asyncio.get_running_loop().create_future()
        self.waits.put_nowait(gate)
        await gate


class GatedReadinessBackend(ReadinessBackend):
    def __init__(self, proposals, manual_requests):
        super().__init__(dict(proposal) for proposal in proposals)
        self.manual_requests = manual_requests
        self.automatic_started = asyncio.Queue()
        self.release_automatic = asyncio.Event()

    async def respond(self, message, **kwargs):
        if (message.endswith("request work.")
                and len(self.requests) >= self.manual_requests):
            self.automatic_started.put_nowait(kwargs["instructions"])
            await self.release_automatic.wait()
        return await super().respond(message, **kwargs)


class JobContinuationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.store = SQLiteJobStore(self.path, timeout=0.01)
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.timer = ControlledHeartbeat()

    async def start(self, proposals=(DELAYED, COMPLETED), *, capacity=1,
                    manual_requests=2, max_steps=3):
        async def wait_forever(_delay):
            await asyncio.Event().wait()

        self.backend = GatedReadinessBackend(proposals, manual_requests)
        self.app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True, jobs_auto_continue=True,
                               jobs_heartbeat_seconds=30,
                               jobs_max_auto_steps=max_steps,
                               jobs_max_concurrent_work=capacity),
            platform_provider=Platform(), cognition_backend=self.backend,
            job_store=self.store, monotonic_clock=self.clock,
            wall_clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
            job_continuation_sleep=self.timer.sleep,
            job_scheduler_sleep=wait_forever,
        )
        self.addAsyncCleanup(self.app.stop)
        await self.app.start()
        self.controller = self.app.job_continuation_controller
        # The task is inspected only to synchronize heartbeat completion/death
        # and to establish that shutdown actually joins it.
        self.heartbeat_task = self.controller._task
        self.timer.gate = await asyncio.wait_for(self.timer.waits.get(), 1)

    async def park(self, name="Recover"):
        binding = self.app.start_job_run(self.store.create_job(name).id)
        await self.app.work_job_run_once(binding.run.id)
        context = self.app._context_for_run(binding.run.id)
        self.assertEqual(context.execution_state, "parked")
        self.assertIs(context.binding.task.status, TaskStatus.PAUSED)
        return context

    async def pulse(self):
        """Wait for the next ordinary sleep, or fail promptly if polling dies."""
        self.assertTrue(self.controller.running)
        self.timer.gate.set_result(None)
        next_wait = asyncio.create_task(self.timer.waits.get())
        try:
            await asyncio.wait((next_wait, self.heartbeat_task), timeout=1,
                               return_when=asyncio.FIRST_COMPLETED)
            error = (None if not self.heartbeat_task.done()
                     else self.heartbeat_task.exception())
            self.assertTrue(self.controller.running,
                            f"heartbeat terminated after offer: {error!r}")
            self.assertTrue(next_wait.done(), "heartbeat did not return to waiting")
            self.timer.gate = next_wait.result()
        finally:
            if not next_wait.done():
                next_wait.cancel()
            await asyncio.gather(next_wait, return_exceptions=True)

    def assert_uncharged(self, context, grant, requests):
        self.assertIs(self.app._context_for_run(context.run_id), context)
        self.assertIs(context.continuation, grant)
        self.assertEqual(context.execution_state, "parked")
        self.assertIs(context.binding.task.status, TaskStatus.PAUSED)
        self.assertEqual(context.task_id, grant.task_id)
        self.assertIsNone(context.active_work_task)
        self.assertEqual(len(self.backend.requests), requests)
        self.assertEqual(self.app.job_work_slots_occupied, 0)
        self.assertFalse(self.app.episode_coordinator.current_autonomous_episodes)
        self.assertIs(self.app.state, LifecycleState.RUNNING)
        self.assertTrue(self.controller.running)

    async def finish_automatic(self, context):
        instructions = await asyncio.wait_for(self.backend.automatic_started.get(), 1)
        self.assertIn(f"JOB{context.job_id}/RUN{context.run_id}", instructions)
        self.assertIn(str(context.task_id), instructions)
        work = context.active_work_task
        self.assertIsNotNone(work)
        self.backend.release_automatic.set()
        await asyncio.wait_for(work, 1)
        return work

    async def test_real_sqlite_lock_preserves_grant_and_recovers_same_run(self):
        await self.start()
        context = await self.park()
        # Seed a previously committed exact-occurrence snapshot, as in the
        # progress tests, to exercise preservation of more than an empty record.
        context.progress = JobProgress(
            context.job_id, context.run_id, context.task_id,
            (JobProgressCounter("checks_completed", 1),),
        )
        grant, binding, progress = context.continuation, context.binding, context.progress
        durable_before = self.store.get_run(context.run_id)
        self.clock.now = grant.eligible_at_monotonic
        lock = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(lock.close)
        lock.execute("BEGIN EXCLUSIVE")
        try:
            with self.assertLogs("embodied_runtime.app", level="ERROR") as logs:
                await self.pulse()
            self.assertIn("database is locked", logs.output[0])
            self.assertIn(f"run=RUN{context.run_id}", logs.output[0])
            self.assert_uncharged(context, grant, 2)
            self.assertIs(context.binding, binding)
            self.assertIs(context.progress, progress)
            self.assertEqual(lock.execute(
                "SELECT status, outcome_summary FROM job_runs WHERE id = ?",
                (context.run_id,),
            ).fetchone(), (durable_before.status.value, durable_before.outcome_summary))
        finally:
            lock.rollback()

        self.assertEqual(self.store.get_run(context.run_id), durable_before)
        await self.pulse()
        self.assertEqual(context.continuation.automatic_steps_remaining, 2)
        self.assertIs(context.progress, progress)
        work = context.active_work_task
        # Another heartbeat while cognition is blocked cannot accept the grant twice.
        await self.pulse()
        self.assertIs(context.active_work_task, work)
        self.assertEqual(context.continuation.automatic_steps_remaining, 2)
        await self.finish_automatic(context)
        self.assertIn("parked progress", self.backend.requests[2][1])
        self.assertIn("checks_completed: 1", self.backend.requests[2][1])
        self.assertIs(self.store.get_run(context.run_id).status, JobRunStatus.COMPLETED)
        self.assertEqual(len(self.store.list_runs(context.job_id)), 1)
        self.assertEqual(len(self.backend.requests), 4)
        await self.pulse()
        self.assertEqual(len(self.backend.requests), 4)
        self.assertEqual(self.app.job_execution_contexts, ())

    async def test_persistence_runtime_error_is_reported_without_revoking_grant(self):
        await self.start()
        context = await self.park()
        grant = context.continuation
        self.clock.now = grant.eligible_at_monotonic
        with patch.object(self.store, "get_run", side_effect=RuntimeError("backend unavailable")):
            with self.assertLogs("embodied_runtime.app", level="ERROR") as logs:
                await self.pulse()
            self.assertIn("backend unavailable", logs.output[0])
        self.assert_uncharged(context, grant, 2)
        await self.pulse()
        await self.finish_automatic(context)
        self.assertIs(self.store.get_run(context.run_id).status, JobRunStatus.COMPLETED)

    async def test_repeated_failures_use_cadence_and_retain_finite_budget(self):
        await self.start((DELAYED, READY), max_steps=1)
        context = await self.park()
        grant = context.continuation
        self.clock.now = grant.eligible_at_monotonic
        with patch.object(self.store, "get_run", side_effect=OSError("unavailable")) as read:
            with self.assertLogs("embodied_runtime.app", level="ERROR") as logs:
                for opportunity in range(1, 5):
                    await self.pulse()
                    self.assertEqual(read.call_count, opportunity)
                    self.assertEqual(self.timer.delays, [30] * (opportunity + 1))
                    self.assert_uncharged(context, grant, 2)
            self.assertEqual(len(logs.records), 1)
            self.assertIsNotNone(logs.records[0].exc_info)

        await self.pulse()
        await self.finish_automatic(context)
        self.assertEqual(context.continuation.automatic_steps_remaining, 0)
        self.assertIs(context.continuation.state, JobContinuationState.AWAITING_OPERATOR)
        self.assertIs(self.store.get_run(context.run_id).status, JobRunStatus.RUNNING)
        await self.pulse()
        await self.pulse()
        self.assertEqual(len(self.backend.requests), 4)

    async def test_failed_sibling_does_not_block_healthy_run_and_recovers_later(self):
        await self.start((DELAYED, DELAYED, COMPLETED, COMPLETED),
                         manual_requests=4)
        first = await self.park("A")
        second = await self.park("B")
        grant = first.continuation
        self.clock.now = grant.eligible_at_monotonic
        get_run = self.store.get_run
        attempts = []

        def read(run_id):
            if run_id == first.run_id:
                attempts.append(run_id)
                raise sqlite3.OperationalError("Run A unavailable")
            return get_run(run_id)

        with patch.object(self.store, "get_run", side_effect=read):
            with self.assertLogs("embodied_runtime.app", level="ERROR") as logs:
                await self.pulse()
                self.assertEqual(self.app.job_work_slots_occupied, 1)
                self.assertIsNone(first.active_work_task)
                self.assertIs(first.continuation, grant)
                await self.finish_automatic(second)
                self.assertIs(get_run(second.run_id).status, JobRunStatus.COMPLETED)
                await self.pulse()
            self.assertEqual(len(logs.records), 1)
            self.assertEqual(attempts, [first.run_id, first.run_id])
            self.assert_uncharged(first, grant, 6)

        self.backend.release_automatic.clear()
        await self.pulse()
        await self.finish_automatic(first)
        self.assertIs(get_run(first.run_id).status, JobRunStatus.COMPLETED)
        self.assertEqual(len(self.backend.requests), 8)
        self.assertEqual(self.app.job_execution_contexts, ())

    async def authority_change_after_failure(self, change):
        await self.start()
        context = await self.park()
        grant = context.continuation
        self.clock.now = grant.eligible_at_monotonic
        with patch.object(self.store, "get_job", side_effect=sqlite3.OperationalError("locked")):
            with self.assertLogs("embodied_runtime.app", level="ERROR"):
                await self.pulse()
        self.assert_uncharged(context, grant, 2)
        if change == "disable":
            self.store.set_job_enabled(context.job_id, False)
        else:
            # Change durable authority while leaving the volatile snapshot running.
            self.store.transition_run(context.run_id, JobRunStatus.STOPPED)
        await self.pulse()
        await self.pulse()
        self.assertEqual(len(self.backend.requests), 2)
        if change == "disable":
            self.assertIs(self.app._context_for_run(context.run_id), context)
            self.assertEqual(context.execution_state, "parked")
            self.assertIs(context.binding.task.status, TaskStatus.PAUSED)
            self.assertIsNone(context.continuation)
            await self.app.finish_job_run_by_id(context.run_id, JobRunStatus.STOPPED)
        self.assertEqual(self.app.job_execution_contexts, ())
        self.assertEqual(self.app.job_work_slots_occupied, 0)
        self.assertFalse(self.app.episode_coordinator.current_autonomous_episodes)
        self.assertIs(self.store.get_run(context.run_id).status, JobRunStatus.STOPPED)

    async def test_disabled_job_is_revalidated_before_recovery(self):
        await self.authority_change_after_failure("disable")

    async def test_stopped_run_is_revalidated_before_recovery(self):
        await self.authority_change_after_failure("stop")

    async def test_shutdown_after_failures_joins_heartbeat_without_later_offers(self):
        await self.start()
        context = await self.park()
        self.clock.now = context.continuation.eligible_at_monotonic
        with patch.object(self.store, "get_run", side_effect=sqlite3.OperationalError("locked")):
            with self.assertLogs("embodied_runtime.app", level="ERROR"):
                await self.pulse()
                await self.pulse()
        gate = self.timer.gate
        await asyncio.wait_for(self.app.stop(), 1)
        self.assertFalse(self.controller.running)
        self.assertTrue(self.heartbeat_task.cancelled())
        self.assertTrue(gate.cancelled())
        self.assertEqual(len(self.backend.requests), 2)
        self.assertEqual(self.app.job_execution_contexts, ())
        self.assertFalse(any(task.get_name().startswith("job-continuation")
                             for task in asyncio.all_tasks()))
        with closing(sqlite3.connect(self.path)) as verification:
            self.assertEqual(verification.execute(
                "SELECT status FROM job_runs WHERE id = ?", (context.run_id,),
            ).fetchone(), ("interrupted",))

    async def test_cancellation_during_normal_wait_propagates(self):
        await self.start()
        await self.park()
        self.heartbeat_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(self.heartbeat_task, 1)
        await self.controller.stop()
        self.assertFalse(self.controller.running)
        self.assertTrue(self.timer.gate.cancelled())
        self.assertEqual(len(self.backend.requests), 2)

    async def test_cancellation_from_offer_propagates_and_does_not_offer_sibling(self):
        await self.start((DELAYED, DELAYED), capacity=2, manual_requests=4)
        first = await self.park("A")
        second = await self.park("B")
        self.clock.now = first.continuation.eligible_at_monotonic
        with patch.object(self.store, "get_job", side_effect=asyncio.CancelledError):
            self.timer.gate.set_result(None)
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(self.heartbeat_task, 1)
        self.assertFalse(self.controller.running)
        self.assertIsNone(first.active_work_task)
        self.assertIsNone(second.active_work_task)
        self.assertEqual(first.continuation.automatic_steps_remaining, 3)
        self.assertEqual(second.continuation.automatic_steps_remaining, 3)
        self.assertEqual(len(self.backend.requests), 4)
        self.assertIsNone(self.app._job_execution_context.get())
        await self.controller.stop()
