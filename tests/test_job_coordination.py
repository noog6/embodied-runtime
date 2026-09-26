from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from embodied_runtime.app import RobotApplication
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import JobRunStatus, JobTarget, SQLiteJobStore
from embodied_runtime.profile import RobotProfile
from embodied_runtime.resources import ResourceKey
from embodied_runtime.tasks import Task, TaskStatus
from tests.test_platform import snapshot


class StaticPlatform:
    def snapshot(self):
        return snapshot()


class RecordingStore:
    def __init__(self, store, *, fail_running=False, fail_terminal=False):
        self.store = store
        self.transitions = []
        self.fail_running = fail_running
        self.fail_terminal = fail_terminal

    def __getattr__(self, name):
        return getattr(self.store, name)

    def transition_run(self, run_id, status, **kwargs):
        self.transitions.append(status)
        if self.fail_running and status is JobRunStatus.RUNNING:
            raise RuntimeError("injected running persistence failure")
        if self.fail_terminal and status in (
            JobRunStatus.COMPLETED, JobRunStatus.FAILED, JobRunStatus.STOPPED
        ):
            raise RuntimeError("injected terminal persistence failure")
        return self.store.transition_run(run_id, status, **kwargs)


class JobCoordinationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.store = SQLiteJobStore(self.path)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.temp.cleanup()

    def make_app(self, store=None):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=StaticPlatform(), job_store=store,
            wall_clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
        )

    async def test_start_creates_one_run_task_and_task_owned_goal(self):
        description = "D" * 1500
        job = self.store.create_job("Review logs", description)
        recording = RecordingStore(self.store)
        app = self.make_app(recording)
        await app.start()

        binding = app.start_job_run(job.id)

        self.assertEqual(recording.transitions, [JobRunStatus.RUNNING])
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.assertEqual(binding.job, job)
        self.assertIs(binding.run.status, JobRunStatus.RUNNING)
        self.assertIs(binding.task, app.current_task)
        self.assertIs(binding.task.status, TaskStatus.RUNNING)
        self.assertEqual(binding.task.description, "Run JOB1: Review logs")
        self.assertEqual(binding.task.goal.description, "Complete JOB1: Review logs")
        self.assertNotIn(description, binding.task.goal.description)
        self.assertEqual(app.active_goal.description, binding.task.goal.description)
        await app.stop()

    async def test_known_preconditions_create_no_runs(self):
        enabled = self.store.create_job("Enabled")
        disabled = self.store.create_job("Disabled", enabled=False)
        app = self.make_app(self.store)
        with self.assertRaisesRegex(RuntimeError, "running application"):
            app.start_job_run(enabled.id)
        self.assertEqual(self.store.list_runs(enabled.id), ())
        await app.start()
        for job_id, message in ((999, "not found"), (disabled.id, "disabled")):
            with self.subTest(job_id=job_id), self.assertRaisesRegex(RuntimeError, message):
                app.start_job_run(job_id)
        app.start_task(Task("unrelated"))
        with self.assertRaisesRegex(RuntimeError, "unrelated Task"):
            app.start_job_run(enabled.id)
        app.stop_task()
        app.set_goal("standalone")
        with self.assertRaisesRegex(RuntimeError, "active goal"):
            app.start_job_run(enabled.id)
        self.assertEqual(self.store.list_runs(enabled.id), ())
        await app.stop()

        no_store = self.make_app()
        await no_store.start()
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            no_store.start_job_run(1)
        await no_store.stop()

    async def test_current_binding_is_exclusive(self):
        first = self.store.create_job("First")
        second = self.store.create_job("Second")
        app = self.make_app(self.store)
        await app.start()
        binding = app.start_job_run(first.id)
        with self.assertRaisesRegex(RuntimeError, "already current"):
            app.start_job_run(second.id)
        self.assertIs(app.current_job_run, binding)
        self.assertIs(app.current_task, binding.task)
        self.assertEqual(self.store.list_runs(second.id), ())
        await app.stop()

    async def test_start_task_failure_stops_pending_run(self):
        job = self.store.create_job("Work")
        app = self.make_app(self.store)
        await app.start()
        with patch.object(app, "start_task", side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                app.start_job_run(job.id)
        run, = self.store.list_runs(job.id)
        self.assertIs(run.status, JobRunStatus.STOPPED)
        self.assertEqual(run.outcome_summary, "Task execution did not start")
        self.assertIsNone(app.current_job_run)
        self.assertIsNone(app.current_task)
        await app.stop()

    async def test_running_transition_failure_cleans_task_and_goal(self):
        job = self.store.create_job("Work")
        recording = RecordingStore(self.store, fail_running=True)
        app = self.make_app(recording)
        await app.start()
        with self.assertRaisesRegex(RuntimeError, "running persistence"):
            app.start_job_run(job.id)
        self.assertIs(self.store.list_runs(job.id)[0].status, JobRunStatus.STOPPED)
        self.assertIsNone(app.current_job_run)
        self.assertIsNone(app.current_task)
        self.assertIsNone(app.active_goal)
        await app.stop()

    async def test_terminal_status_and_summary_mapping(self):
        app = self.make_app(self.store)
        await app.start()
        for run_status, task_status, summary_fields in (
            (JobRunStatus.COMPLETED, TaskStatus.COMPLETED, ("done", None)),
            (JobRunStatus.FAILED, TaskStatus.FAILED, (None, "broken")),
            (JobRunStatus.STOPPED, TaskStatus.STOPPED, ("operator stop", None)),
        ):
            with self.subTest(status=run_status):
                job = self.store.create_job(f"Work {run_status.value}")
                binding = app.start_job_run(job.id)
                app.acquire_task_resource(ResourceKey("camera"))
                summary = summary_fields[0] or summary_fields[1]
                finished = app.finish_job_run(run_status, summary)
                self.assertIs(finished.task.status, task_status)
                self.assertEqual(
                    (finished.run.outcome_summary, finished.run.error_summary),
                    summary_fields,
                )
                self.assertIsNone(app.current_task)
                self.assertIsNone(app.current_job_run)
                self.assertIsNone(app.active_goal)
                self.assertEqual(app.resources.leases_for(
                    app._task_resource_owner(binding.task)), ())
        await app.stop()

    async def test_pause_resume_layers_under_running_job(self):
        job = self.store.create_job("Work")
        app = self.make_app(self.store)
        await app.start()
        app.start_job_run(job.id)
        app.pause_task()
        self.assertIs(app.current_job_run.run.status, JobRunStatus.RUNNING)
        self.assertIs(app.current_job_run.task.status, TaskStatus.PAUSED)
        with self.assertRaises(Exception):
            app.finish_job_run(JobRunStatus.COMPLETED)
        self.assertIs(self.store.list_runs(job.id)[0].status, JobRunStatus.RUNNING)
        app.resume_task()
        self.assertIs(app.current_job_run.run.status, JobRunStatus.RUNNING)
        app.finish_job_run(JobRunStatus.COMPLETED)
        await app.stop()

    async def test_paused_stop_succeeds(self):
        job = self.store.create_job("Work")
        app = self.make_app(self.store)
        await app.start()
        app.start_job_run(job.id)
        app.pause_task()
        finished = app.finish_job_run(JobRunStatus.STOPPED, "paused stop")
        self.assertIs(finished.task.status, TaskStatus.STOPPED)
        await app.stop()

    async def test_terminal_persistence_failure_retains_matching_retry(self):
        job = self.store.create_job("Work")
        recording = RecordingStore(self.store, fail_terminal=True)
        app = self.make_app(recording)
        await app.start()
        app.start_job_run(job.id)
        with self.assertRaisesRegex(RuntimeError, "terminal persistence"):
            app.finish_job_run(JobRunStatus.COMPLETED, "done")
        self.assertIsNone(app.current_task)
        self.assertIs(app.current_job_run.task.status, TaskStatus.COMPLETED)
        self.assertIs(self.store.list_runs(job.id)[0].status, JobRunStatus.RUNNING)
        with self.assertRaisesRegex(RuntimeError, "must remain completed"):
            app.finish_job_run(JobRunStatus.FAILED, "wrong")
        recording.fail_terminal = False
        finished = app.finish_job_run(JobRunStatus.COMPLETED, "done")
        self.assertIs(finished.run.status, JobRunStatus.COMPLETED)
        self.assertIsNone(app.current_job_run)
        await app.stop()

    async def test_shutdown_interrupts_without_recovering(self):
        job = self.store.create_job("Work", target=JobTarget("body", "sprayer"))
        app = self.make_app(self.store)
        await app.start()
        binding = app.start_job_run(job.id)
        await app.stop()
        self.assertIsNone(app.current_task)
        self.assertIsNone(app.current_job_run)

        reopened = SQLiteJobStore(self.path)
        persisted = reopened.get_run(binding.run.id)
        self.assertIs(persisted.status, JobRunStatus.INTERRUPTED)
        self.assertIsNotNone(persisted.finished_at)
        self.assertIsNone(persisted.result_report)
        self.assertEqual(reopened.get_job(job.id).target, JobTarget("body", "sprayer"))
        restarted = self.make_app(reopened)
        await restarted.start()
        self.assertIsNone(restarted.current_task)
        self.assertIsNone(restarted.current_job_run)
        await restarted.stop()

    async def test_startup_reconciles_orphans_once_without_volatile_bindings(self):
        job = self.store.create_job("History")
        completed = self.store.create_run(job.id)
        self.store.transition_run(completed.id, JobRunStatus.RUNNING)
        self.store.transition_run(completed.id, JobRunStatus.COMPLETED, result_report="ok")
        running = self.store.create_run(job.id)
        self.store.transition_run(running.id, JobRunStatus.RUNNING)
        pending = self.store.create_run(job.id)
        stopped = self.store.create_run(job.id)
        self.store.transition_run(stopped.id, JobRunStatus.STOPPED)
        app = self.make_app(self.store)
        with self.assertLogs("embodied_runtime.app", level="INFO") as captured:
            await app.start()
        statuses = tuple(run.status for run in self.store.list_runs(job.id))
        self.assertEqual(statuses, (
            JobRunStatus.COMPLETED, JobRunStatus.INTERRUPTED,
            JobRunStatus.INTERRUPTED, JobRunStatus.STOPPED,
        ))
        self.assertIsNone(app.current_job_run)
        self.assertIsNone(app.current_task)
        joined = "\n".join(captured.output)
        self.assertIn(f"run=RUN{running.id} status=interrupted", joined)
        self.assertIn("previous_status=running", joined)
        self.assertIn(f"run=RUN{pending.id} status=interrupted", joined)
        self.assertIn("previous_status=pending", joined)
        first_finished = self.store.get_run(running.id).finished_at
        await app.stop()
        reopened = SQLiteJobStore(self.path)
        restarted = self.make_app(reopened)
        await restarted.start()
        self.assertEqual(reopened.get_run(running.id).finished_at, first_finished)
        await restarted.stop()

    async def test_shutdown_does_not_overwrite_terminal_result_from_quiescence(self):
        job = self.store.create_job("Race")
        app = self.make_app(self.store)
        await app.start()
        binding = app.start_job_run(job.id)

        async def finish_while_quiescing():
            self.store.transition_run(
                binding.run.id, JobRunStatus.COMPLETED,
                outcome_summary="finished in time", result_report="legitimate result",
            )

        with patch.object(app, "_stop_job_work", side_effect=finish_while_quiescing):
            await app.stop()
        reopened = SQLiteJobStore(self.path)
        try:
            persisted = reopened.get_run(binding.run.id)
            self.assertIs(persisted.status, JobRunStatus.COMPLETED)
            self.assertEqual(persisted.result_report, "legitimate result")
        finally:
            reopened.close()

    async def test_startup_reconciliation_failure_fails_closed(self):
        class FailingReconciliationStore(RecordingStore):
            def interrupt_nonterminal_runs(self):
                raise RuntimeError("injected reconciliation failure")

        app = self.make_app(FailingReconciliationStore(self.store))
        with self.assertRaisesRegex(RuntimeError, "injected reconciliation failure"):
            await app.start()
        self.assertEqual(app.state.value, "stopped")
        self.assertIsNone(app.current_job_run)
        self.assertIsNone(app.current_task)


if __name__ == "__main__":
    unittest.main()
