from pathlib import Path
import sqlite3
import tempfile
import unittest

from embodied_runtime.jobs import JobTriggerType, SQLiteJobStore


class JobTriggerStoreTests(unittest.TestCase):
    def test_trigger_round_trip_and_atomic_active_coalescing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite3"
            store = SQLiteJobStore(path)
            job = store.create_job("Tend to power")
            trigger = store.set_trigger(job.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
            self.assertEqual(store.get_trigger(job.id, trigger.event_type), trigger)
            first = store.create_triggered_run(job.id)
            self.assertIsNotNone(first)
            self.assertIsNone(store.create_triggered_run(job.id))
            store.close()
            reopened = SQLiteJobStore(path)
            self.assertEqual(reopened.list_triggers(), (trigger,))
            reopened.close()

    def test_unsupported_trigger_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteJobStore(Path(temporary) / "jobs.sqlite3")
            job = store.create_job("Tend")
            with self.assertRaises(TypeError):
                store.set_trigger(job.id, "anything")  # type: ignore[arg-type]
            store.close()

    def test_only_one_enabled_owner_per_trigger(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteJobStore(Path(temporary) / "jobs.sqlite3")
            first = store.create_job("First")
            second = store.create_job("Second")
            store.set_trigger(first.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
            store.set_job_enabled(first.id, False)
            with self.assertRaises(ValueError):
                store.set_trigger(second.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
            store.remove_trigger(first.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
            self.assertEqual(
                store.set_trigger(second.id, JobTriggerType.POWER_ATTENTION_REQUIRED).job_id,
                second.id)
            store.close()

    def test_pre_trigger_v4_database_migrates_transactionally_and_idempotently(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                    CREATE TABLE jobs (
                      id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                      enabled INTEGER NOT NULL, target_kind TEXT, target_identifier TEXT,
                      created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                    CREATE TABLE job_runs (
                      id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
                      status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
                      finished_at TEXT, outcome_summary TEXT, error_summary TEXT,
                      result_report TEXT);
                    CREATE INDEX idx_job_runs_job ON job_runs(job_id,id);
                    CREATE TABLE job_schedules (
                      job_id INTEGER PRIMARY KEY REFERENCES jobs(id), enabled INTEGER NOT NULL,
                      local_time TEXT NOT NULL, timezone TEXT NOT NULL,
                      last_started_local_date TEXT);
                    INSERT INTO jobs VALUES(1,'Existing','preserved',1,NULL,NULL,
                      '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
                    INSERT INTO job_schedules VALUES(1,1,'02:00','UTC','2026-01-01');
                    PRAGMA user_version=4;
                """)
            store = SQLiteJobStore(path)
            self.assertEqual(store.get_job(1).description, "preserved")
            self.assertEqual(store.get_schedule(1).local_time, "02:00")
            self.assertEqual(store._connection.execute(
                "PRAGMA user_version").fetchone()[0], 5)
            store.close()
            reopened = SQLiteJobStore(path)
            self.assertEqual(reopened.list_triggers(), ())
            reopened.close()
