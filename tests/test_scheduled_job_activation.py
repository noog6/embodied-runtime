import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import JobContinuationState, JobRunStatus, SQLiteJobStore
from embodied_runtime.profile import RobotProfile
from embodied_runtime.tasks import Task
from tests.test_job_continuation import SequencedBackend
from tests.test_job_execution import BlockingBackend, JobBackend, Platform


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class ScheduledJobActivationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.store = SQLiteJobStore(self.path)
        self.clock = MutableClock(datetime.fromisoformat("2026-09-21T01:59:00-04:00"))

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.temp.cleanup()

    def app(self, backend=None, *, store=None, auto_continue=False):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(
                initiative_enabled=True, initiative_goal_closure_enabled=True,
                jobs_auto_continue=auto_continue, jobs_max_auto_steps=3,
            ),
            platform_provider=Platform(), cognition_backend=backend,
            job_store=store or self.store, timezone_name="America/Toronto",
            wall_clock=self.clock,
        )

    async def settle_work(self, app):
        task = app._active_job_work_task
        self.assertIsNotNone(task)
        await task

    async def test_not_due_then_exactly_due(self):
        backend = JobBackend("completed")
        job = self.store.create_job("Due")
        self.store.set_schedule(job.id, "02:00", "America/Toronto")
        app = self.app(backend)
        await app.start()
        await app._offer_scheduled_job()
        self.assertEqual(self.store.list_runs(job.id), ())
        self.assertIsNone(app.current_task)
        self.assertEqual(backend.requests, [])
        self.clock.value = datetime.fromisoformat("2026-09-21T02:00:00-04:00")
        await app._offer_scheduled_job()
        await self.settle_work(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.assertIs(self.store.list_runs(job.id)[0].status, JobRunStatus.COMPLETED)
        await app.stop()

    async def test_catch_up_is_once_and_does_not_backfill(self):
        backend = JobBackend("completed")
        job = self.store.create_job("Catch up")
        self.store.set_schedule(job.id, "02:00", "America/Toronto")
        # An old marker represents arbitrarily many missed days.
        self.store.create_scheduled_run(job.id, "2026-09-17")
        self.clock.value = datetime.fromisoformat("2026-09-21T08:00:00-04:00")
        app = self.app(backend)
        await app.start()
        await app._offer_scheduled_job()
        await self.settle_work(app)  # immediate startup offer
        await app._offer_scheduled_job()
        self.assertEqual(len(self.store.list_runs(job.id)), 2)
        self.assertEqual(self.store.get_schedule(job.id).last_started_local_date,
                         "2026-09-21")
        await app.stop()

    async def test_disabled_first_does_not_starve_second(self):
        schedule_disabled = self.store.create_job("Schedule disabled")
        job_disabled = self.store.create_job("Job disabled", enabled=False)
        eligible = self.store.create_job("Eligible")
        self.store.set_schedule(schedule_disabled.id, "00:30", "America/Toronto",
                                enabled=False)
        self.store.set_schedule(job_disabled.id, "01:00", "America/Toronto")
        self.store.set_schedule(eligible.id, "02:00", "America/Toronto")
        self.clock.value = datetime.fromisoformat("2026-09-21T03:00:00-04:00")
        app = self.app(JobBackend("completed"))
        await app.start()
        await app._offer_scheduled_job()
        await self.settle_work(app)
        for skipped in (schedule_disabled, job_disabled):
            self.assertEqual(self.store.list_runs(skipped.id), ())
            self.assertIsNone(self.store.get_schedule(skipped.id).last_started_local_date)
        self.assertEqual(len(self.store.list_runs(eligible.id)), 1)
        await app.stop()

    async def test_global_blocker_consumes_neither_then_starts_only_earliest(self):
        jobs = [self.store.create_job(name) for name in ("First", "Second")]
        for hour, job in enumerate(jobs, 1):
            self.store.set_schedule(job.id, f"0{hour}:00", "America/Toronto")
        self.clock.value = datetime.fromisoformat("2026-09-21T03:00:00-04:00")
        app = self.app(JobBackend("completed"))
        app.episode_coordinator._operator_waiters = 1
        await app.start()
        await asyncio.sleep(0)
        self.assertTrue(all(not self.store.list_runs(job.id) for job in jobs))
        self.assertTrue(all(self.store.get_schedule(job.id).last_started_local_date is None
                            for job in jobs))
        app.episode_coordinator._operator_waiters = 0
        await app._offer_scheduled_job()
        await self.settle_work(app)
        self.assertEqual(len(self.store.list_runs(jobs[0].id)), 1)
        self.assertEqual(self.store.list_runs(jobs[1].id), ())
        await app.stop()

    async def test_task_and_goal_are_global_deferrals(self):
        app = self.app(JobBackend())
        await app.start()
        for blocker in ("task", "goal"):
            with self.subTest(blocker=blocker):
                job = self.store.create_job(f"Due {blocker}")
                self.store.set_schedule(job.id, "01:00", "America/Toronto")
                self.clock.value = datetime.fromisoformat("2026-09-21T03:00:00-04:00")
                if blocker == "task":
                    app.start_task(Task("unrelated"))
                else:
                    app.set_goal("unrelated")
                await app._offer_scheduled_job()
                self.assertEqual(self.store.list_runs(job.id), ())
                self.assertIsNone(self.store.get_schedule(job.id).last_started_local_date)
                if blocker == "task":
                    app.stop_task()
                else:
                    app.clear_goal()
        await app.stop()

    async def test_current_job_is_a_global_deferral(self):
        current = self.store.create_job("Current")
        due = self.store.create_job("Due")
        app = self.app(JobBackend())
        await app.start()
        app.start_job_run(current.id)
        self.store.set_schedule(due.id, "01:00", "America/Toronto")
        self.clock.value = datetime.fromisoformat("2026-09-21T03:00:00-04:00")
        await app._offer_scheduled_job()
        self.assertEqual(self.store.list_runs(due.id), ())
        self.assertIsNone(self.store.get_schedule(due.id).last_started_local_date)
        await app.stop()

    async def test_cognition_unavailable_does_not_consume_date(self):
        job = self.store.create_job("Wait for cognition")
        self.store.set_schedule(job.id, "01:00", "America/Toronto")
        self.clock.value = datetime.fromisoformat("2026-09-21T03:00:00-04:00")
        app = self.app()
        await app.start()
        await app._offer_scheduled_job()
        self.assertEqual(self.store.list_runs(job.id), ())
        self.assertIsNone(self.store.get_schedule(job.id).last_started_local_date)
        self.assertIsNone(app.current_task)
        self.assertIsNone(app.active_goal)
        await app.stop()
        reopened = SQLiteJobStore(self.path)
        app = self.app(JobBackend("completed"), store=reopened)
        await app.start()
        with self.assertLogs("embodied_runtime.app", level="INFO") as logs:
            await app._offer_scheduled_job()
            await self.settle_work(app)
        self.assertTrue(any("work=started source=scheduled" in line
                            for line in logs.output))
        self.assertEqual(len(reopened.list_runs(job.id)), 1)
        await app.stop()

    async def test_scheduled_completion_and_phase_four_handoff(self):
        job = self.store.create_job("Continue once")
        self.store.set_schedule(job.id, "01:00", "America/Toronto")
        self.clock.value = datetime.fromisoformat("2026-09-21T03:00:00-04:00")
        backend = SequencedBackend(("continue", "completed"))
        app = self.app(backend, auto_continue=True)
        await app.start()
        with self.assertLogs("embodied_runtime.app", level="INFO") as logs:
            await app._offer_scheduled_job()
            await self.settle_work(app)
        self.assertTrue(any("work=started source=scheduled" in line
                            for line in logs.output))
        self.assertTrue(any("continuation=armed" in line and "source=scheduled" in line
                            for line in logs.output))
        self.assertIs(self.store.list_runs(job.id)[0].status, JobRunStatus.RUNNING)
        self.assertIs(app.job_continuation.state, JobContinuationState.ARMED)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        self.assertNotIn("Previous bounded Job work", backend.requests[0][1])
        app._offer_job_continuation()
        await self.settle_work(app)
        self.assertEqual(backend.requests[2][1].count("Previous bounded Job work"), 1)
        self.assertIn("bounded result", backend.requests[2][1])
        self.assertIs(self.store.list_runs(job.id)[0].status, JobRunStatus.COMPLETED)
        self.assertIsNone(app.job_continuation)
        await app.stop()

    async def test_provider_failure_consumes_date_without_scheduler_retry(self):
        class FailingBackend(JobBackend):
            async def respond(self, *args, **kwargs):
                self.requests.append(args)
                raise RuntimeError("provider failed")

        job = self.store.create_job("Fail once")
        self.store.set_schedule(job.id, "01:00", "America/Toronto")
        self.clock.value = datetime.fromisoformat("2026-09-21T03:00:00-04:00")
        app = self.app(FailingBackend())
        await app.start()
        await app._offer_scheduled_job()
        await self.settle_work(app)
        await app._offer_scheduled_job()
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.assertEqual(self.store.get_schedule(job.id).last_started_local_date,
                         "2026-09-21")
        await app.stop()

    async def test_shutdown_detaches_running_scheduled_work_and_restart_does_not_adopt(self):
        job = self.store.create_job("Blocking")
        self.store.set_schedule(job.id, "01:00", "America/Toronto")
        self.clock.value = datetime.fromisoformat("2026-09-21T03:00:00-04:00")
        backend = BlockingBackend()
        app = self.app(backend)
        await app.start()
        await backend.started.wait()
        await app.stop()
        self.assertFalse(app.scheduled_job_controller.running)
        self.assertIsNone(app.current_job_run)
        reopened = SQLiteJobStore(self.path)
        self.assertIs(reopened.list_runs(job.id)[0].status, JobRunStatus.RUNNING)
        app = self.app(JobBackend("completed"), store=reopened)
        await app.start()
        self.assertIsNone(app.current_job_run)
        self.assertIsNone(app.current_task)
        await app._offer_scheduled_job()
        self.assertEqual(len(reopened.list_runs(job.id)), 1)
        self.assertIsNone(app.current_job_run)
        await app.stop()


if __name__ == "__main__":
    unittest.main()
