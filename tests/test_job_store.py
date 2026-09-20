import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from embodied_runtime.jobs import (
    InvalidJobRunTransitionError, JobRunStatus, JobTarget, SQLiteJobStore,
)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 20, tzinfo=UTC)

    def __call__(self):
        self.value += timedelta(seconds=1)
        return self.value


class SQLiteJobStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.clock = Clock()
        self.store = SQLiteJobStore(self.path, clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_restart_preserves_job_target_enabled_state_and_run(self):
        job = self.store.create_job("Keep subjects in frame", "Camera duty",
                                    target=JobTarget("body", "camera"))
        self.store.set_job_enabled(job.id, False)
        run = self.store.create_run(job.id)
        self.store.transition_run(run.id, JobRunStatus.RUNNING)
        completed = self.store.transition_run(
            run.id, JobRunStatus.COMPLETED, outcome_summary="Reviewed"
        )
        self.store.close()
        reopened = SQLiteJobStore(self.path)
        persisted = reopened.get_job(job.id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.name, job.name)
        self.assertEqual(persisted.description, "Camera duty")
        self.assertEqual(persisted.target, JobTarget("body", "camera"))
        self.assertFalse(persisted.enabled)
        self.assertEqual(reopened.get_run(run.id), completed)
        reopened.close()
        self.store = SQLiteJobStore(self.path)

    def test_catalog_is_global_and_filter_is_explicit(self):
        targets = [JobTarget("body", name) for name in ("camera", "arms", "sprayer")]
        jobs = [self.store.create_job(name.title(), target=target)
                for name, target in zip(("camera", "arms", "sprayer"), targets)]
        self.assertEqual(self.store.list_jobs(), tuple(jobs))
        self.assertEqual(self.store.list_jobs(target=targets[0]), (jobs[0],))
        self.assertEqual(self.store.list_jobs(), tuple(jobs))

    def test_unassigned_round_trips_as_none(self):
        job = self.store.create_job("Await assignment", target=None)
        self.assertIsNone(self.store.get_job(job.id).target)

    def test_orphan_runs_are_rejected_by_foreign_key(self):
        with self.assertRaises(ValueError):
            self.store.create_run(999)
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO job_runs(job_id,status,created_at) VALUES(999,'pending','x')"
                )

    def test_every_legal_transition(self):
        job = self.store.create_job("Duty")
        for first, second in ((JobRunStatus.PENDING, JobRunStatus.STOPPED),
                              (JobRunStatus.RUNNING, JobRunStatus.COMPLETED),
                              (JobRunStatus.RUNNING, JobRunStatus.FAILED),
                              (JobRunStatus.RUNNING, JobRunStatus.STOPPED)):
            run = self.store.create_run(job.id)
            if first is JobRunStatus.RUNNING:
                run = self.store.transition_run(run.id, first)
                self.assertIsNotNone(run.started_at)
            terminal = self.store.transition_run(run.id, second)
            self.assertIsNotNone(terminal.finished_at)

    def test_terminal_and_double_terminal_transitions_fail_closed(self):
        job = self.store.create_job("Duty")
        for terminal in (JobRunStatus.COMPLETED, JobRunStatus.FAILED,
                         JobRunStatus.STOPPED):
            run = self.store.create_run(job.id)
            if terminal is not JobRunStatus.STOPPED:
                self.store.transition_run(run.id, JobRunStatus.RUNNING)
            self.store.transition_run(run.id, terminal)
            for attempted in (JobRunStatus.RUNNING, terminal):
                with self.subTest(terminal=terminal, attempted=attempted), \
                     self.assertRaises(InvalidJobRunTransitionError):
                    self.store.transition_run(run.id, attempted)

    def test_unsupported_schema_fails_closed(self):
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA user_version = 99")
        with self.assertRaises(RuntimeError):
            SQLiteJobStore(self.path)
        self.store = SQLiteJobStore(":memory:")


if __name__ == "__main__":
    unittest.main()
