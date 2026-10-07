import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.console import RuntimeConsole
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import JobProgress, JobProgressCounter, JobRunStatus, SQLiteJobStore
from embodied_runtime.profile import RobotProfile
from embodied_runtime.tasks import TaskStatus
from tests.test_job_continuation_recovery import ControlledHeartbeat, DELAYED
from tests.test_job_execution import Platform
from tests.test_job_readiness import Clock, ReadinessBackend


class ParkedJobOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.store = SQLiteJobStore(self.path)
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.timer = ControlledHeartbeat()

    def make_app(self, backend, store=None):
        async def wait_forever(_delay):
            await asyncio.Event().wait()

        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True, jobs_auto_continue=True,
                               jobs_max_concurrent_work=2, jobs_heartbeat_seconds=30),
            platform_provider=Platform(), cognition_backend=backend,
            job_store=store or self.store, monotonic_clock=self.clock,
            wall_clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
            job_continuation_sleep=self.timer.sleep,
            job_scheduler_sleep=wait_forever,
        )
        self.addAsyncCleanup(app.stop)
        return app

    async def start(self, proposals=(DELAYED,)):
        self.backend = ReadinessBackend(dict(proposal) for proposal in proposals)
        self.app = self.make_app(self.backend)
        await self.app.start()
        self.controller = self.app.job_continuation_controller
        self.heartbeat = self.controller._task
        self.timer.gate = await asyncio.wait_for(self.timer.waits.get(), 1)

    async def park(self, name="Retain"):
        binding = self.app.start_job_run(self.store.create_job(name).id)
        await self.app.work_current_job_once()
        context = self.app._context_for_run(binding.run.id)
        self.assertEqual(context.execution_state, "parked")
        self.assertIs(context.binding.task.status, TaskStatus.PAUSED)
        return context

    def disable(self, context):
        result, _ = RuntimeConsole(self.app).execute(f"job disable JOB{context.job_id}")
        self.assertIn("disabled", result)

    async def reject_explicit(self):
        requests = len(self.backend.requests)
        with self.assertRaisesRegex(RuntimeError, "authority is stale"):
            await self.app.work_current_job_once()
        self.assertEqual(len(self.backend.requests), requests)

    async def pulse(self):
        self.timer.gate.set_result(None)
        next_wait = asyncio.create_task(self.timer.waits.get())
        try:
            await asyncio.wait((next_wait, self.heartbeat), timeout=1,
                               return_when=asyncio.FIRST_COMPLETED)
            self.assertTrue(self.controller.running, "restoration killed the heartbeat")
            self.assertTrue(next_wait.done(), "heartbeat did not return to waiting")
            self.timer.gate = next_wait.result()
        finally:
            if not next_wait.done():
                next_wait.cancel()
            await asyncio.gather(next_wait, return_exceptions=True)

    def assert_retained(self, context):
        self.assertIs(self.app._context_for_run(context.run_id), context)
        self.assertEqual(context.execution_state, "parked")
        self.assertIs(context.binding.task.status, TaskStatus.PAUSED)
        self.assertIs(context.task_binding.task, context.binding.task)
        self.assertIsNone(context.task_binding.active_goal)
        self.assertIsNone(context.continuation)
        self.assertIsNone(context.active_work_task)
        self.assertIs(self.store.get_run(context.run_id).status, JobRunStatus.RUNNING)

    def persisted(self, run_id):
        with SQLiteJobStore(self.path) as verification:
            return verification.get_run(run_id)

    async def test_disable_rejected_explicit_restore_keeps_exact_stop_and_durable_result(self):
        await self.start()
        context = await self.park()
        task_id = context.task_id
        self.disable(context)
        await self.reject_explicit()

        stopped = await self.app.finish_job_run_by_id(
            context.run_id, JobRunStatus.STOPPED, "operator stopped disabled work")

        self.assertEqual(stopped.task.id, task_id)
        self.assertIs(stopped.task.status, TaskStatus.STOPPED)
        self.assertIs(stopped.run.status, JobRunStatus.STOPPED)
        self.assertIsNone(self.app._context_for_run(context.run_id))
        self.assertEqual(len(self.backend.requests), 2)
        await self.app.stop()
        durable = self.persisted(context.run_id)
        self.assertEqual(durable, stopped.run)
        self.assertEqual(durable.outcome_summary, "operator stopped disabled work")
        self.assertIsNotNone(durable.finished_at)

    async def test_disabled_run_keeps_complete_and_fail_control_without_cognition(self):
        await self.start((DELAYED, DELAYED))
        for status in (JobRunStatus.COMPLETED, JobRunStatus.FAILED):
            with self.subTest(status=status):
                context = await self.park(status.value)
                self.disable(context)
                await self.reject_explicit()
                self.assert_retained(context)
                requests = len(self.backend.requests)
                finished = await self.app.finish_job_run_by_id(
                    context.run_id, status, "operator decision")
                self.assertIs(finished.run.status, status)
                self.assertIs(finished.task.status, TaskStatus(status.value))
                self.assertEqual(len(self.backend.requests), requests)
                self.assertEqual(self.store.get_run(context.run_id), finished.run)

    async def test_disabled_eligible_heartbeat_retains_owner_and_shutdown_interrupts(self):
        await self.start()
        context = await self.park()
        self.clock.now = context.continuation.eligible_at_monotonic
        self.disable(context)
        await self.pulse()
        self.assert_retained(context)
        await self.pulse()
        self.assertEqual(len(self.backend.requests), 2)
        self.assertEqual(self.app.job_work_slots_occupied, 0)
        await self.app.stop()
        self.assertTrue(self.heartbeat.cancelled())
        self.assertIs(self.persisted(context.run_id).status, JobRunStatus.INTERRUPTED)

    async def test_shutdown_after_explicit_rejection_interrupts_retained_run(self):
        await self.start()
        context = await self.park()
        self.disable(context)
        await self.reject_explicit()
        self.assert_retained(context)
        await self.reject_explicit()
        self.assert_retained(context)
        await self.app.stop()
        durable = self.persisted(context.run_id)
        self.assertIs(durable.status, JobRunStatus.INTERRUPTED)
        self.assertIsNotNone(durable.finished_at)
        self.assertIsNone(durable.outcome_summary)
        self.assertIsNone(durable.result_report)
        self.assertEqual(len(self.backend.requests), 2)

    async def test_terminal_write_failure_retains_exact_identity_and_matching_retry(self):
        await self.start()
        context = await self.park()
        task_id = context.task_id
        self.disable(context)
        await self.reject_explicit()
        with patch.object(self.store, "transition_run", side_effect=sqlite3.OperationalError("terminal write failed")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "terminal write failed"):
                await self.app.finish_job_run_by_id(context.run_id, JobRunStatus.STOPPED)
        self.assertIs(self.app._context_for_run(context.run_id), context)
        self.assertEqual(context.task_id, task_id)
        self.assertIs(context.binding.task.status, TaskStatus.STOPPED)
        self.assertIsNone(context.task_binding)
        self.assertIs(self.store.get_run(context.run_id).status, JobRunStatus.RUNNING)
        with self.assertRaisesRegex(RuntimeError, "must remain stopped"):
            await self.app.finish_job_run_by_id(context.run_id, JobRunStatus.COMPLETED)
        await self.pulse()
        self.assertEqual(len(self.backend.requests), 2)
        stopped = await self.app.finish_job_run_by_id(context.run_id, JobRunStatus.STOPPED)
        self.assertIs(stopped.run.status, JobRunStatus.STOPPED)
        self.assertEqual(stopped.task.id, task_id)
        self.assertEqual(self.app.job_execution_contexts, ())
        await self.app.stop()
        self.assertEqual(self.persisted(context.run_id), stopped.run)

    async def test_failed_shutdown_write_reports_failure_and_startup_reconciles(self):
        await self.start()
        context = await self.park()
        self.disable(context)
        await self.reject_explicit()
        with patch.object(self.store, "transition_run", side_effect=sqlite3.OperationalError("interruption write failed")):
            with self.assertLogs("embodied_runtime.app", level="ERROR") as logs:
                with self.assertRaisesRegex(sqlite3.OperationalError, "interruption write failed"):
                    await self.app.stop()
        self.assertIn("interruption=failed", logs.output[0])
        self.assertFalse(self.controller.running)
        self.assertIs(self.persisted(context.run_id).status, JobRunStatus.RUNNING)

        reopened = SQLiteJobStore(self.path)
        self.addCleanup(reopened.close)
        backend = ReadinessBackend(())
        fresh = self.make_app(backend, reopened)
        await fresh.start()
        self.assertIs(reopened.get_run(context.run_id).status, JobRunStatus.INTERRUPTED)
        self.assertEqual(fresh.job_execution_contexts, ())
        self.assertEqual(backend.requests, [])

    async def test_reenable_requires_explicit_work_and_old_stop_unblocks_new_activation(self):
        await self.start((DELAYED, DELAYED, DELAYED))
        context = await self.park()
        self.clock.now = context.continuation.eligible_at_monotonic
        await self.pulse()
        if context.active_work_task is not None:
            await asyncio.wait_for(context.active_work_task, 1)
        self.assertEqual(context.execution_state, "parked")
        self.assertEqual(context.continuation.automatic_steps_remaining, 2)
        grant = context.continuation
        self.clock.now = grant.eligible_at_monotonic
        self.disable(context)
        await self.reject_explicit()
        RuntimeConsole(self.app).execute(f"job enable JOB{context.job_id}")
        await self.pulse()
        await self.pulse()
        self.assert_retained(context)
        self.assertEqual(len(self.backend.requests), 4)
        with self.assertRaisesRegex(RuntimeError, "active occurrence"):
            self.app.start_job_run(context.job_id)
        await self.app.work_current_job_once()
        self.assertEqual(context.task_id, grant.task_id)
        self.assertEqual(len(self.backend.requests), 6)
        self.assertEqual(context.continuation.automatic_steps_remaining, 3)
        self.assertIsNot(context.continuation, grant)
        await self.app.finish_job_run_by_id(context.run_id, JobRunStatus.STOPPED)
        fresh = self.app.start_job_run(context.job_id)
        self.assertNotEqual(fresh.run.id, context.run_id)
        self.assertNotEqual(fresh.task.id, context.task_id)
        self.assertIs(fresh.run.status, JobRunStatus.RUNNING)
        self.assertIsNone(self.app.job_continuation)
        self.assertEqual(len(self.store.list_runs(context.job_id)), 2)

    async def test_reenable_resume_does_not_rearm_revoked_grant(self):
        await self.start()
        context = await self.park()
        self.disable(context)
        await self.reject_explicit()
        self.store.set_job_enabled(context.job_id, True)
        resumed = self.app.resume_task()
        self.assertEqual(resumed.id, context.task_id)
        self.assertIs(resumed.status, TaskStatus.RUNNING)
        self.assertIsNone(context.continuation)
        self.clock.now = 1000
        await self.pulse()
        self.assertEqual(len(self.backend.requests), 2)

    async def test_exact_stop_unblocks_durable_triggered_admission_after_reenable(self):
        await self.start()
        context = await self.park()
        self.disable(context)
        await self.reject_explicit()
        self.store.set_job_enabled(context.job_id, True)
        self.assertIsNone(self.store.create_triggered_run(context.job_id))
        await self.app.finish_job_run_by_id(context.run_id, JobRunStatus.STOPPED)
        admitted = self.store.create_triggered_run(context.job_id)
        self.assertIsNotNone(admitted)
        self.assertNotEqual(admitted.id, context.run_id)
        self.assertIs(admitted.status, JobRunStatus.PENDING)
        self.assertIs(self.store.get_run(context.run_id).status, JobRunStatus.STOPPED)
        self.assertEqual(len(self.backend.requests), 2)
        # This store-level admission probe has no Task; clean up its temporary
        # pending occurrence rather than attributing it to application ownership.
        self.store.transition_run(admitted.id, JobRunStatus.STOPPED)

    async def test_disable_rejection_and_exact_stop_preserve_parked_sibling(self):
        later = dict(DELAYED, delay_seconds=600)
        await self.start((DELAYED, later))
        first = await self.park("A")
        second = await self.park("B")
        second.progress = JobProgress(second.job_id, second.run_id, second.task_id,
                                     (JobProgressCounter("checks_completed", 1),))
        binding, task_binding, grant, progress = (
            second.binding, second.task_binding, second.continuation, second.progress)
        durable = self.store.get_run(second.run_id)
        self.disable(first)
        self.clock.now = first.continuation.eligible_at_monotonic
        await self.pulse()
        self.assert_retained(first)
        await self.app.finish_job_run_by_id(first.run_id, JobRunStatus.STOPPED)
        self.assertIs(self.app._context_for_run(second.run_id), second)
        self.assertIs(second.binding, binding)
        self.assertIs(second.task_binding, task_binding)
        self.assertIs(second.continuation, grant)
        self.assertIs(second.progress, progress)
        self.assertEqual(second.execution_state, "parked")
        self.assertEqual(grant.automatic_steps_remaining, 3)
        self.assertEqual(self.store.get_run(second.run_id), durable)
        self.assertEqual(len(self.backend.requests), 4)

    async def test_already_terminal_durable_run_is_not_resurrected_or_overwritten(self):
        await self.start()
        context = await self.park()
        durable = self.store.transition_run(
            context.run_id, JobRunStatus.COMPLETED,
            outcome_summary="external completion", result_report="immutable report")
        self.disable(context)
        await self.reject_explicit()
        self.assertIsNone(self.app._context_for_run(context.run_id))
        self.assertEqual(len(self.backend.requests), 2)
        await self.app.stop()
        self.assertEqual(self.persisted(context.run_id), durable)

    async def test_missing_job_retains_known_nonterminal_run_for_exact_stop(self):
        await self.start()
        context = await self.park()
        with patch.object(self.store, "get_job", return_value=None):
            await self.reject_explicit()
        self.assert_retained(context)
        stopped = await self.app.finish_job_run_by_id(context.run_id, JobRunStatus.STOPPED)
        self.assertIs(stopped.run.status, JobRunStatus.STOPPED)

    async def test_missing_run_drops_stale_context_without_starting_work(self):
        await self.start()
        context = await self.park()
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM job_runs WHERE id = ?", (context.run_id,))
        connection.close()
        await self.reject_explicit()
        self.assertIsNone(self.app._context_for_run(context.run_id))
        self.assertEqual(len(self.backend.requests), 2)
        await self.app.stop()
        self.assertIsNone(self.persisted(context.run_id))

    async def test_stale_continuation_binding_keeps_run_for_shutdown(self):
        await self.start()
        context = await self.park()
        context.continuation = replace(context.continuation, task_id=uuid4())
        await self.reject_explicit()
        self.assertIs(self.app._context_for_run(context.run_id), context)
        self.assertIsNone(context.continuation)
        self.assertEqual(len(self.backend.requests), 2)
        await self.app.stop()
        self.assertIs(self.persisted(context.run_id).status, JobRunStatus.INTERRUPTED)
