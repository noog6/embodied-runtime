import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from embodied_runtime.app import (
    ApplicationOptions, CurrentJobRun, RobotApplication, _CurrentTaskBinding,
)
from embodied_runtime.console import RuntimeConsole
from embodied_runtime.earcons import Earcon
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    InvalidJobRunTransitionError, JobProgress, JobProgressCounter,
    JobRunStatus, SQLiteJobStore,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.tasks import Task, TaskStatus
from tests.test_earcons import RecordingEarcons
from tests.test_job_concurrent_slots import ParkingBackend, Platform
from tests.test_job_readiness import Clock


class TerminalWriteFaultStore:
    """Fail selected terminal writes before SQLite changes durable authority."""

    def __init__(self, store):
        self.store = store
        self.fail_runs = set()
        self.attempts = []

    def __getattr__(self, name):
        return getattr(self.store, name)

    def transition_run(self, run_id, status, **kwargs):
        self.attempts.append((run_id, status))
        if run_id in self.fail_runs and status in (
            JobRunStatus.COMPLETED, JobRunStatus.FAILED, JobRunStatus.STOPPED,
        ):
            raise sqlite3.OperationalError("injected terminal write failure")
        return self.store.transition_run(run_id, status, **kwargs)


class RecordingParkingBackend(ParkingBackend):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.tool_calls = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions))

        async def record_tool(call):
            self.tool_calls.append((instructions, call))
            return await tool_executor(call)

        return await super().respond(
            message, instructions=instructions, tools=tools,
            tool_executor=record_tool, refreshed_instructions=refreshed_instructions,
        )


class ExactTerminalRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.store = SQLiteJobStore(self.path)
        self.addCleanup(self.store.close)
        self.faults = TerminalWriteFaultStore(self.store)
        self.clock = Clock()

    async def start(self):
        async def wait_forever(_delay):
            await asyncio.Event().wait()

        self.backend = RecordingParkingBackend()
        self.earcons = RecordingEarcons()
        self.app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True, jobs_auto_continue=True,
                               jobs_max_concurrent_work=3),
            platform_provider=Platform(), cognition_backend=self.backend,
            job_store=self.faults, earcon_player=self.earcons,
            monotonic_clock=self.clock,
            wall_clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
            job_continuation_sleep=wait_forever, job_scheduler_sleep=wait_forever,
        )
        self.addAsyncCleanup(self.app.stop)
        await self.app.start()

    def new_run(self, name):
        binding = self.app.start_job_run(self.store.create_job(name).id)
        context = self.app._context_for_run(binding.run.id)
        self.backend.gate(self.source(context))
        return context

    @staticmethod
    def source(context):
        return f"JOB{context.job_id}/RUN{context.run_id}"

    async def park(self, context):
        self.backend.release[self.source(context)].set()
        await self.app.work_job_run_once(context.run_id)
        self.assertEqual(context.execution_state, "parked")
        self.assertIs(context.binding.task.status, TaskStatus.PAUSED)
        self.assertEqual(context.continuation.automatic_steps_remaining, 3)

    def snapshot(self, context):
        return {**{field: getattr(context, field) for field in (
            "binding", "task_binding", "continuation", "progress", "active_work_task",
        )}, "execution_state": context.execution_state,
            "durable": self.store.get_run(context.run_id)}

    def assert_unchanged(self, context, before):
        self.assertIs(self.app._context_for_run(context.run_id), context)
        for field in ("binding", "task_binding", "continuation", "progress", "active_work_task"):
            self.assertIs(getattr(context, field), before[field], field)
        self.assertEqual(context.execution_state, before["execution_state"])
        self.assertEqual(self.store.get_run(context.run_id), before["durable"])

    async def fail_terminal_write(self, context, status, *, summary="decision", report="result"):
        self.faults.fail_runs.add(context.run_id)
        with self.assertRaisesRegex(sqlite3.OperationalError, "terminal write failure"):
            await self.app.finish_job_run_by_id(context.run_id, status, summary,
                                                result_report=report)
        self.assertIs(self.app._context_for_run(context.run_id), context)
        self.assertIs(context.binding.task.status, TaskStatus(status.value))
        self.assertIsNone(context.task_binding)
        self.assertIsNone(context.continuation)
        self.assertIsNone(context.active_work_task)
        self.assertIs(self.store.get_run(context.run_id).status, JobRunStatus.RUNNING)
        self.assertIsNone(self.app._job_execution_context.get())

    async def assert_matching_retry(self, status, *, failed_attempts=1):
        await self.start()
        first = self.new_run("A")
        sibling = self.new_run("B")
        before = self.snapshot(sibling)
        identity = (first.job_id, first.run_id, first.task_id)
        summary, report = f"{status.value} decision", f"{status.value} report"
        counter = f"job_runs_{status.value}"
        for _ in range(failed_attempts):
            await self.fail_terminal_write(first, status, summary=summary, report=report)
            self.assertEqual((first.job_id, first.run_id, first.task_id), identity)
            self.assert_unchanged(sibling, before)
            self.assertIs(self.app.current_task, sibling.binding.task)
            self.assertEqual(self.app.observability.snapshot()["metrics"][counter], 0)
            if self.app._earcon_tasks:
                await asyncio.gather(*tuple(self.app._earcon_tasks))
            self.assertNotIn(Earcon.WORK_COMPLETED, self.earcons.cues)
        conflicting = JobRunStatus.FAILED if status is not JobRunStatus.FAILED else JobRunStatus.COMPLETED
        with self.assertRaisesRegex(RuntimeError, f"must remain {status.value}"):
            await self.app.finish_job_run_by_id(first.run_id, conflicting)
        self.faults.fail_runs.remove(first.run_id)
        requests, tools = list(self.backend.requests), list(self.backend.tool_calls)

        finished = await self.app.finish_job_run_by_id(
            first.run_id, status, summary, result_report=report)

        self.assertEqual(finished.task.id, identity[2])
        self.assertIs(finished.task.status, TaskStatus(status.value))
        self.assertIs(finished.run.status, status)
        self.assertEqual((finished.run.outcome_summary, finished.run.error_summary),
                         (None, summary) if status is JobRunStatus.FAILED else (summary, None))
        self.assertEqual(finished.run.result_report, report)
        self.assertIsNotNone(finished.run.finished_at)
        self.assertIsNone(self.app._context_for_run(first.run_id))
        self.assert_unchanged(sibling, before)
        self.assertIs(self.app.current_job_execution_context, sibling)
        self.assertIs(self.app.current_task, sibling.binding.task)
        self.assertIsNone(self.app._job_execution_context.get())
        self.assertEqual(self.backend.requests, requests)
        self.assertEqual(self.backend.tool_calls, tools)
        self.assertEqual(self.app.observability.snapshot()["metrics"][counter], 1)
        if self.app._earcon_tasks:
            await asyncio.gather(*tuple(self.app._earcon_tasks))
        self.assertEqual(self.earcons.cues.count(Earcon.WORK_COMPLETED),
                         int(status is JobRunStatus.COMPLETED))
        self.assertEqual(self.faults.attempts.count((first.run_id, status)), failed_attempts + 1)
        with SQLiteJobStore(self.path) as verification:
            self.assertEqual(verification.get_run(first.run_id), finished.run)
            self.assertEqual(verification.get_run(sibling.run_id), before["durable"])
        await self.app.stop()
        with SQLiteJobStore(self.path) as verification:
            self.assertEqual(verification.get_run(first.run_id), finished.run)
            self.assertIs(verification.get_run(sibling.run_id).status, JobRunStatus.INTERRUPTED)

    async def test_matching_retry_succeeds_while_sibling_remains_foreground_running(self):
        await self.assert_matching_retry(JobRunStatus.COMPLETED)

    async def test_stopped_retry_survives_repeated_failures_and_conflicting_decisions(self):
        await self.assert_matching_retry(JobRunStatus.STOPPED, failed_attempts=3)

    async def test_completed_retry_survives_repeated_failures_and_conflicting_decisions(self):
        await self.assert_matching_retry(JobRunStatus.COMPLETED, failed_attempts=3)

    async def test_failed_retry_survives_repeated_failures_and_conflicting_decisions(self):
        await self.assert_matching_retry(JobRunStatus.FAILED, failed_attempts=3)

    async def test_retry_preserves_inflight_and_parked_siblings_without_semantic_replay(self):
        await self.start()
        first = self.new_run("A")
        await self.park(first)
        parked = self.new_run("C")
        await self.park(parked)
        parked.progress = JobProgress(parked.job_id, parked.run_id, parked.task_id,
                                     (JobProgressCounter("checks_completed", 2),))
        active = self.new_run("B")
        work = asyncio.create_task(self.app.work_job_run_once(active.run_id))
        await asyncio.wait_for(self.backend.started[self.source(active)].wait(), 1)
        before_active, before_parked = self.snapshot(active), self.snapshot(parked)
        self.assertIs(active.active_work_task, work)
        self.assertEqual(self.app.job_work_slots_occupied, 1)
        await self.fail_terminal_write(first, JobRunStatus.COMPLETED)
        self.faults.fail_runs.remove(first.run_id)
        requests, tools = list(self.backend.requests), list(self.backend.tool_calls)
        self.assertEqual(len(tools), 2)  # A and C each accepted one work outcome.

        finished = await self.app.finish_job_run_by_id(
            first.run_id, JobRunStatus.COMPLETED, "decision", result_report="result")

        self.assert_unchanged(active, before_active)
        self.assert_unchanged(parked, before_parked)
        self.assertIs(self.app.current_task, active.binding.task)
        self.assertFalse(work.done())
        self.assertIn(self.source(active), self.backend.outstanding)
        self.assertEqual(self.app.job_work_slots_occupied, 1)
        self.assertEqual(len(self.app.episode_coordinator.current_autonomous_episodes), 1)
        self.assertEqual(self.backend.requests, requests)
        self.assertEqual(self.backend.tool_calls, tools)
        with SQLiteJobStore(self.path) as verification:
            self.assertEqual(verification.get_run(first.run_id), finished.run)
            self.assertEqual(verification.get_run(active.run_id), before_active["durable"])
            self.assertEqual(verification.get_run(parked.run_id), before_parked["durable"])
        await self.app.stop()
        self.assertTrue(work.cancelled())
        self.assertEqual(self.app.job_execution_contexts, ())
        with SQLiteJobStore(self.path) as verification:
            self.assertEqual(verification.get_run(first.run_id), finished.run)
            for sibling in (active, parked):
                self.assertIs(verification.get_run(sibling.run_id).status, JobRunStatus.INTERRUPTED)

    async def test_disabled_restore_owner_can_retry_stop_while_sibling_is_live(self):
        await self.start()
        first = self.new_run("A")
        await self.park(first)
        RuntimeConsole(self.app).execute(f"job disable JOB{first.job_id}")
        with self.assertRaisesRegex(RuntimeError, "authority is stale"):
            await self.app.work_current_job_once()
        self.assertIs(self.app._context_for_run(first.run_id), first)
        self.assertIs(first.binding.task.status, TaskStatus.PAUSED)
        self.assertIsNone(first.continuation)
        sibling = self.new_run("B")
        before = self.snapshot(sibling)
        requests, tools = list(self.backend.requests), list(self.backend.tool_calls)
        await self.fail_terminal_write(first, JobRunStatus.STOPPED)
        self.faults.fail_runs.remove(first.run_id)
        stopped = await self.app.finish_job_run_by_id(first.run_id, JobRunStatus.STOPPED,
                                                    "disabled work stopped")
        self.assertIs(stopped.run.status, JobRunStatus.STOPPED)
        self.assert_unchanged(sibling, before)
        self.assertEqual(self.backend.requests, requests)
        self.assertEqual(self.backend.tool_calls, tools)

    async def test_target_live_binding_is_rejected_even_when_foreground_has_no_task(self):
        await self.start()
        first = self.new_run("A")
        original_binding = first.task_binding
        sibling = self.new_run("B")
        await self.fail_terminal_write(first, JobRunStatus.COMPLETED)
        await self.fail_terminal_write(sibling, JobRunStatus.STOPPED)
        self.assertIsNone(self.app.current_task)
        self.faults.fail_runs.clear()
        before = self.snapshot(sibling)
        attempts = list(self.faults.attempts)
        # Corrupt only A's activation binding; B's absent activation must not
        # authorize a terminal retry of an inconsistent target context.
        for invalid in (original_binding, _CurrentTaskBinding(first.binding.task, None),
                        _CurrentTaskBinding(sibling.binding.task, None)):
            with self.subTest(binding=invalid):
                first.task_binding = invalid
                try:
                    with self.assertRaisesRegex(RuntimeError, "Task binding is inconsistent"):
                        await self.app.finish_job_run_by_id(first.run_id, JobRunStatus.COMPLETED)
                    self.assertIs(self.store.get_run(first.run_id).status, JobRunStatus.RUNNING)
                    self.assertEqual(self.faults.attempts, attempts)
                    self.assert_unchanged(sibling, before)
                finally:
                    first.task_binding = None
        finished = await self.app.finish_job_run_by_id(first.run_id, JobRunStatus.COMPLETED)
        self.assertIs(finished.run.status, JobRunStatus.COMPLETED)
        self.assert_unchanged(sibling, before)

    async def test_retry_revalidates_durable_ownership_and_preserves_task_identity(self):
        await self.start()
        first = self.new_run("A")
        sibling = self.new_run("B")
        before = self.snapshot(sibling)
        await self.fail_terminal_write(first, JobRunStatus.STOPPED)
        self.faults.fail_runs.clear()
        with self.assertRaisesRegex(RuntimeError, "identity cannot change"):
            first.replace_binding(CurrentJobRun(first.binding.job, first.binding.run, Task("other")))
        wrong_owner = replace(self.store.get_run(first.run_id), job_id=sibling.job_id)
        with patch.object(self.faults, "get_run", return_value=wrong_owner):
            with self.assertRaisesRegex(RuntimeError, "durable ownership"):
                await self.app.finish_job_run_by_id(first.run_id, JobRunStatus.STOPPED)
        self.assertIs(self.app._context_for_run(first.run_id), first)
        self.assert_unchanged(sibling, before)
        # A caller's installed B context must likewise neither authorize nor
        # reject A's exact retry, and must survive that call unchanged.
        token = self.app._job_execution_context.set(sibling)
        try:
            finished = await self.app.finish_job_run_by_id(first.run_id, JobRunStatus.STOPPED)
            self.assertIs(self.app._job_execution_context.get(), sibling)
        finally:
            self.app._job_execution_context.reset(token)
        self.assertIs(finished.run.status, JobRunStatus.STOPPED)
        self.assert_unchanged(sibling, before)

    async def test_already_terminal_durable_results_remain_immutable(self):
        await self.start()
        for durable_status in (JobRunStatus.COMPLETED, JobRunStatus.FAILED):
            with self.subTest(durable_status=durable_status):
                first = self.new_run("A")
                sibling = self.new_run("B")
                before = self.snapshot(sibling)
                await self.fail_terminal_write(first, JobRunStatus.COMPLETED)
                self.faults.fail_runs.remove(first.run_id)
                fields = ({"error_summary": "external decision"}
                          if durable_status is JobRunStatus.FAILED
                          else {"outcome_summary": "external decision"})
                durable = self.store.transition_run(first.run_id, durable_status,
                                                   result_report="immutable report", **fields)
                with self.assertRaises(InvalidJobRunTransitionError):
                    await self.app.finish_job_run_by_id(first.run_id, JobRunStatus.COMPLETED,
                                                        "replacement", result_report="replacement")
                self.assertEqual(self.store.get_run(first.run_id), durable)
                self.assertIs(self.app._context_for_run(first.run_id), first)
                self.assert_unchanged(sibling, before)
        await self.app.stop()
        with SQLiteJobStore(self.path) as verification:
            self.assertEqual(verification.get_run(first.run_id), durable)
