import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from embodied_runtime.jobs import (
    MAX_RUN_REPORT_CHARS, InvalidJobRunTransitionError, JobRunStatus, JobTarget,
    SQLiteJobStore,
)
from embodied_runtime.jobs.model import MAX_JOB_DESCRIPTION_CHARS


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
            run.id, JobRunStatus.COMPLETED, outcome_summary="Reviewed",
            result_report=(
                "No failed application entries were found. The previous run ended "
                "with an operator interrupt and completed cleanup with no non-daemon threads."
            ),
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
        self.assertEqual(reopened.get_latest_completed_run(job.id), completed)
        reopened.close()
        self.store = SQLiteJobStore(self.path)

    def test_description_update_persists_across_restart_and_preserves_metadata(self):
        original = self.store.create_job(
            "Review logs", "Old instructions", enabled=False,
            target=JobTarget("agent", "mira"),
        )
        updated = self.store.set_job_description(original.id, "  New instructions.  ")
        self.assertEqual(updated.description, "New instructions.")
        self.assertEqual(updated.id, original.id)
        self.assertEqual(updated.name, original.name)
        self.assertEqual(updated.enabled, original.enabled)
        self.assertEqual(updated.target, original.target)
        self.assertEqual(updated.created_at, original.created_at)
        self.assertGreater(updated.updated_at, original.updated_at)

        self.store.close()
        reopened = SQLiteJobStore(self.path)
        self.assertEqual(reopened.get_job(original.id), updated)
        reopened.close()
        self.store = SQLiteJobStore(self.path)

    def test_description_update_rejects_invalid_or_unknown_without_mutation(self):
        original = self.store.create_job("Review logs", "Keep this")
        with self.assertRaisesRegex(ValueError, "at most 2000"):
            self.store.set_job_description(
                original.id, "x" * (MAX_JOB_DESCRIPTION_CHARS + 1)
            )
        self.assertEqual(self.store.get_job(original.id), original)
        with self.assertRaises(KeyError):
            self.store.set_job_description(999, "Nothing")
        self.assertEqual(self.store.get_job(original.id), original)

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

    def test_latest_completed_ignores_newer_failed_stopped_and_running_runs(self):
        job = self.store.create_job("Review")
        completed = self.store.create_run(job.id)
        self.store.transition_run(completed.id, JobRunStatus.RUNNING)
        completed = self.store.transition_run(
            completed.id, JobRunStatus.COMPLETED, outcome_summary="done",
            result_report="Detailed findings.",
        )
        for status in (JobRunStatus.FAILED, JobRunStatus.STOPPED):
            run = self.store.create_run(job.id)
            self.store.transition_run(run.id, JobRunStatus.RUNNING)
            self.store.transition_run(run.id, status, result_report="other")
        running = self.store.create_run(job.id)
        self.store.transition_run(running.id, JobRunStatus.RUNNING)
        self.assertEqual(self.store.get_latest_completed_run(job.id), completed)
        self.assertIsNone(self.store.get_latest_completed_run(
            self.store.create_job("Never completed").id
        ))

    def test_reports_are_terminal_bounded_and_immutable(self):
        job = self.store.create_job("Duty")
        run = self.store.create_run(job.id)
        with self.assertRaisesRegex(ValueError, "non-terminal"):
            self.store.transition_run(
                run.id, JobRunStatus.RUNNING, result_report="too early"
            )
        self.store.transition_run(run.id, JobRunStatus.RUNNING)
        failed = self.store.transition_run(
            run.id, JobRunStatus.FAILED, error_summary="failed",
            result_report="Useful failure analysis.",
        )
        self.assertEqual(failed.result_report, "Useful failure analysis.")
        with self.assertRaises(InvalidJobRunTransitionError):
            self.store.transition_run(
                run.id, JobRunStatus.FAILED, result_report="replacement"
            )
        run = self.store.create_run(job.id)
        self.store.transition_run(run.id, JobRunStatus.RUNNING)
        with self.assertRaisesRegex(ValueError, "at most 8000"):
            self.store.transition_run(
                run.id, JobRunStatus.COMPLETED,
                result_report="x" * (MAX_RUN_REPORT_CHARS + 1),
            )

    def test_version_two_migration_preserves_historical_run_with_null_report(self):
        self.store.close()
        self.path.unlink()
        with sqlite3.connect(self.path) as connection:
            connection.executescript("""
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                    enabled INTEGER NOT NULL, target_kind TEXT, target_identifier TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE job_runs (
                    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
                    status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
                    finished_at TEXT, outcome_summary TEXT, error_summary TEXT);
                CREATE INDEX idx_job_runs_job ON job_runs(job_id, id);
                CREATE TABLE job_schedules (
                    job_id INTEGER PRIMARY KEY REFERENCES jobs(id), enabled INTEGER NOT NULL,
                    local_time TEXT NOT NULL, timezone TEXT NOT NULL,
                    last_started_local_date TEXT);
                INSERT INTO jobs VALUES(1,'Old job','',1,NULL,NULL,
                    '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
                INSERT INTO job_runs VALUES(1,1,'completed','2026-01-01T00:00:00Z',
                    '2026-01-01T00:00:01Z','2026-01-01T00:00:02Z','old result',NULL);
                PRAGMA user_version = 2;
            """)
        self.store = SQLiteJobStore(self.path)
        run = self.store.get_run(1)
        self.assertIs(run.status, JobRunStatus.COMPLETED)
        self.assertEqual(run.outcome_summary, "old result")
        self.assertIsNone(run.result_report)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_unsupported_schema_fails_closed(self):
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA user_version = 99")
        with self.assertRaises(RuntimeError):
            SQLiteJobStore(self.path)
        self.store = SQLiteJobStore(":memory:")


if __name__ == "__main__":
    unittest.main()
