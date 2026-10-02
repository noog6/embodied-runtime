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

    def test_runtime_ready_trigger_round_trips_in_new_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteJobStore(Path(temporary) / "jobs.sqlite3")
            job = store.create_job("Inspect capabilities")
            trigger = store.set_trigger(job.id, JobTriggerType.RUNTIME_READY)
            self.assertEqual(store.get_trigger(job.id, JobTriggerType.RUNTIME_READY), trigger)
            self.assertEqual(store._connection.execute(
                "PRAGMA user_version").fetchone()[0], 9)
            store.close()

    def test_unsupported_trigger_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteJobStore(Path(temporary) / "jobs.sqlite3")
            job = store.create_job("Tend")
            with self.assertRaises(TypeError):
                store.set_trigger(job.id, "anything")  # type: ignore[arg-type]
            store.close()

    def test_multiple_jobs_may_own_the_same_trigger(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteJobStore(Path(temporary) / "jobs.sqlite3")
            first = store.create_job("First")
            second = store.create_job("Second")
            store.set_trigger(first.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
            second_trigger = store.set_trigger(
                second.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
            store.remove_trigger(first.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
            self.assertEqual(store.list_triggers(), (second_trigger,))
            store.close()

    def test_health_triggers_are_durable_and_one_job_may_own_both(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite3"
            store = SQLiteJobStore(path)
            job = store.create_job("Tend runtime health")
            thermal = store.set_trigger(job.id, JobTriggerType.THERMAL_WARNING_RAISED)
            memory = store.set_trigger(job.id, JobTriggerType.MEMORY_PRESSURE_RAISED)
            other = store.create_job("Other")
            other_thermal = store.set_trigger(
                other.id, JobTriggerType.THERMAL_WARNING_RAISED)
            store.close()
            reopened = SQLiteJobStore(path)
            self.assertEqual(set(reopened.list_triggers()), {thermal, memory, other_thermal})
            reopened.close()

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
                "PRAGMA user_version").fetchone()[0], 9)
            store.close()
            reopened = SQLiteJobStore(path)
            self.assertEqual(reopened.list_triggers(), ())
            reopened.close()

    def test_v5_migration_preserves_trigger_and_removes_unique_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite3"
            stamp = "2026-01-01T00:00:00Z"
            with sqlite3.connect(path) as connection:
                connection.executescript(f"""
                    CREATE TABLE jobs (
                      id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                      enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                      target_kind TEXT, target_identifier TEXT,
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
                    CREATE TABLE job_triggers (
                      job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
                      event_type TEXT NOT NULL CHECK(event_type IN ('power_attention_required')),
                      enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                      PRIMARY KEY(job_id,event_type));
                    CREATE UNIQUE INDEX idx_job_triggers_enabled_event
                      ON job_triggers(event_type) WHERE enabled=1;
                    INSERT INTO jobs VALUES(12,'Runtime health','',1,NULL,NULL,'{stamp}','{stamp}');
                    INSERT INTO job_triggers VALUES(12,'power_attention_required',1);
                    PRAGMA user_version=5;
                """)
            store = SQLiteJobStore(path)
            expected = store.get_trigger(12, JobTriggerType.POWER_ATTENTION_REQUIRED)
            self.assertIsNotNone(expected)
            self.assertTrue(expected.enabled)
            self.assertEqual(expected.job_id, 12)
            self.assertEqual(store._connection.execute(
                "PRAGMA user_version").fetchone()[0], 9)
            other = store.create_job("Other")
            store.set_trigger(other.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
            store.set_trigger(12, JobTriggerType.THERMAL_WARNING_RAISED)
            store.set_trigger(12, JobTriggerType.MEMORY_PRESSURE_RAISED)
            store.close()
            reopened = SQLiteJobStore(path)
            self.assertEqual(reopened.get_trigger(
                12, JobTriggerType.POWER_ATTENTION_REQUIRED), expected)
            self.assertEqual(len(reopened.list_triggers()), 4)
            reopened.close()

    def test_v6_migration_preserves_existing_trigger_and_accepts_runtime_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite3"
            seed = SQLiteJobStore(path)
            job = seed.create_job("Runtime health")
            existing = seed.set_trigger(job.id, JobTriggerType.THERMAL_WARNING_RAISED)
            seed._connection.execute("PRAGMA user_version=6")
            # Recreate the former v6 constraint from the current v8 schema.
            seed._connection.execute("ALTER TABLE job_triggers RENAME TO triggers_v7")
            seed._connection.execute("""CREATE TABLE job_triggers (
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
                event_type TEXT NOT NULL CHECK(event_type IN ('power_attention_required',
                    'thermal_warning_raised','memory_pressure_raised')),
                enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                PRIMARY KEY(job_id,event_type))""")
            seed._connection.execute("INSERT INTO job_triggers SELECT * FROM triggers_v7")
            seed._connection.execute("DROP TABLE triggers_v7")
            seed._connection.execute("""CREATE UNIQUE INDEX idx_job_triggers_enabled_event
                ON job_triggers(event_type) WHERE enabled=1""")
            seed.close()

            migrated = SQLiteJobStore(path)
            self.assertEqual(migrated.list_triggers(), (existing,))
            second = migrated.create_job("Capabilities")
            migrated.set_trigger(second.id, JobTriggerType.RUNTIME_READY)
            self.assertEqual(migrated._connection.execute(
                "PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(len(migrated.list_triggers()), 2)
            migrated.close()

    def test_v7_migration_preserves_trigger_and_removes_global_unique_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite3"
            seed = SQLiteJobStore(path)
            first = seed.create_job("Existing")
            second = seed.create_job("Second")
            expected = seed.set_trigger(first.id, JobTriggerType.RUNTIME_READY)
            seed._connection.execute("""CREATE UNIQUE INDEX
                idx_job_triggers_enabled_event ON job_triggers(event_type)
                WHERE enabled=1""")
            seed._connection.execute("PRAGMA user_version=7")
            seed.close()

            migrated = SQLiteJobStore(path)
            self.assertEqual(migrated.get_trigger(
                first.id, JobTriggerType.RUNTIME_READY), expected)
            self.assertEqual(migrated._connection.execute(
                "PRAGMA user_version").fetchone()[0], 9)
            self.assertIsNone(migrated._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND "
                "name='idx_job_triggers_enabled_event'").fetchone())
            migrated.set_trigger(second.id, JobTriggerType.RUNTIME_READY)
            self.assertEqual(len(migrated.list_triggers(
                JobTriggerType.RUNTIME_READY)), 2)
            migrated.close()

            reopened = SQLiteJobStore(path)
            self.assertEqual(len(reopened.list_triggers(
                JobTriggerType.RUNTIME_READY)), 2)
            self.assertEqual(reopened._connection.execute(
                "PRAGMA user_version").fetchone()[0], 9)
            reopened.close()
