import json
import asyncio
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
import sqlite3

from embodied_runtime.jobs import JobSchedule, ScheduledJobController, SQLiteJobStore
from embodied_runtime.run_history import MAX_DAILY_RUNS, RunHistoryEvidenceReader


class JobScheduleStoreTests(unittest.TestCase):
    def test_schedule_persists_and_daily_occurrence_is_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite3"
            store = SQLiteJobStore(path)
            job = store.create_job("Review logs")
            schedule = store.set_schedule(job.id, "02:00", "America/Toronto")
            self.assertEqual(schedule, JobSchedule(job.id, True, "02:00", "America/Toronto"))
            first = store.create_scheduled_run(job.id, "2026-09-21")
            self.assertIsNotNone(first)
            self.assertIsNone(store.create_scheduled_run(job.id, "2026-09-21"))
            store.close()
            reopened = SQLiteJobStore(path)
            self.assertEqual(reopened.get_schedule(job.id).last_started_local_date,
                             "2026-09-21")
            self.assertEqual(len(reopened.list_runs(job.id)), 1)
            self.assertTrue(reopened.remove_schedule(job.id))
            self.assertIsNotNone(reopened.get_job(job.id))
            self.assertEqual(len(reopened.list_runs(job.id)), 1)
            reopened.close()

    def test_schedule_validation_is_strict(self):
        for value in ("2:00", "24:00", "02:60", "02:00 "):
            with self.assertRaises(ValueError):
                JobSchedule(1, True, value, "UTC")
        with self.assertRaises(ValueError):
            JobSchedule(1, True, "02:00", "Not/A_Zone")

    def test_schema_v1_migrates_without_rewriting_job_or_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE jobs (
                  id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                  enabled INTEGER NOT NULL, target_kind TEXT, target_identifier TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE job_runs (
                  id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, status TEXT NOT NULL,
                  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                  outcome_summary TEXT, error_summary TEXT);
                CREATE INDEX idx_job_runs_job ON job_runs(job_id, id);
                PRAGMA user_version = 1;
            """)
            stamp = "2026-09-20T00:00:00.000000Z"
            connection.execute(
                "INSERT INTO jobs VALUES(1,'Existing','preserve me',1,NULL,NULL,?,?)",
                (stamp, stamp),
            )
            connection.execute(
                "INSERT INTO job_runs VALUES(1,1,'running',?,?,NULL,NULL,NULL)",
                (stamp, stamp),
            )
            connection.commit()
            connection.close()
            store = SQLiteJobStore(path)
            self.assertEqual(store.get_job(1).description, "preserve me")
            self.assertEqual(store.get_run(1).status.value, "running")
            self.assertEqual(store.get_run(1).created_at, datetime(2026, 9, 20, tzinfo=UTC))
            self.assertEqual(store.set_schedule(1, "02:00", "America/Toronto").job_id, 1)
            self.assertEqual(store._connection.execute("PRAGMA user_version").fetchone()[0], 4)
            store.close()


class ScheduledJobControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_offer_survives_failure_then_waits_before_retry(self):
        calls = 0
        sleeps = []
        gates = []

        async def offer():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient")

        async def sleep(delay):
            sleeps.append(delay)
            gate = asyncio.Event()
            gates.append(gate)
            await gate.wait()

        controller = ScheduledJobController(30, offer, sleep=sleep)
        controller.start()
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)  # immediate, before first sleep
        self.assertTrue(controller.running)
        self.assertEqual(sleeps, [30])
        gates[0].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(calls, 2)
        self.assertTrue(controller.running)
        await controller.stop()
        self.assertFalse(controller.running)


class PreviousDayEvidenceTests(unittest.TestCase):
    @staticmethod
    def _write_run(root, number, lines):
        directory = root / f"R{number}"
        directory.mkdir()
        (directory / "run.json").write_text(json.dumps({
            "schema_version": 1, "run_id": f"R{number}", "run_number": number,
            "started_at": "2026-11-01T00:00:00+00:00", "ended_at": None,
            "status": "started", "exit_code": None, "profile": "test",
            "hardware": "virtual", "config_source": None,
        }))
        (directory / "runtime.log").write_text("".join(lines))

    def test_filters_line_calendar_date_across_long_running_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "R1"
            directory.mkdir()
            (directory / "run.json").write_text(json.dumps({
                "schema_version": 1, "run_id": "R1", "run_number": 1,
                "started_at": "2026-09-19T03:00:00+00:00", "ended_at": None,
                "status": "started", "exit_code": None, "profile": "test",
                "hardware": "virtual", "config_source": None,
            }))
            (directory / "runtime.log").write_text(
                "2026-09-20T03:59:59.000Z [APP] failed=old\n"
                "2026-09-20T04:00:00.000Z [APP] status=ready\n"
                "2026-09-21T03:59:59.000Z [JOBS] status=failed\n"
                "2026-09-21T04:00:00.000Z [APP] status=current\n"
            )
            reader = RunHistoryEvidenceReader(
                root, "R1", timezone_name="America/Toronto",
                clock=lambda: datetime.fromisoformat("2026-09-21T02:00:00-04:00"),
            )
            overview = reader.inspect("overview", "previous_day")
            self.assertEqual(overview["calendar_date"], "2026-09-20")
            self.assertEqual(overview["safe_line_count"], 2)
            self.assertEqual(overview["run_ids"], ["R1"])
            search = reader.inspect("search", "previous_day", "failed")
            self.assertEqual([(item["run_id"], item["line_number"])
                              for item in search["matches"]], [("R1", 3)])

    def test_toronto_fall_back_day_uses_local_calendar_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_run(root, 1, (
                "2026-11-01T03:59:59.000Z [APP] status=before\n",
                "2026-11-01T04:00:00.000Z [APP] status=start\n",
                "2026-11-02T04:59:59.000Z [APP] status=end\n",
                "2026-11-02T05:00:00.000Z [APP] status=after\n",
            ))
            result = RunHistoryEvidenceReader(
                root, timezone_name="America/Toronto",
                clock=lambda: datetime.fromisoformat("2026-11-02T02:00:00-05:00"),
            ).inspect("overview", "previous_day")
            self.assertEqual(result["calendar_date"], "2026-11-01")
            self.assertEqual(result["safe_line_count"], 2)
            self.assertIn("status=start", result["first_lines"][0]["text"])
            self.assertIn("status=end", result["last_lines"][-1]["text"])

    def test_daily_run_cap_uses_newest_runs_and_reports_truncation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            line = "2026-09-20T16:00:00.000Z [APP] status=ready\n"
            for number in range(1, MAX_DAILY_RUNS + 3):
                self._write_run(root, number, (line,))
            result = RunHistoryEvidenceReader(
                root, timezone_name="America/Toronto",
                clock=lambda: datetime.fromisoformat("2026-09-21T02:00:00-04:00"),
            ).inspect("overview", "previous_day")
            self.assertEqual(result["run_ids"][0], "R3")
            self.assertEqual(result["run_ids"][-1], f"R{MAX_DAILY_RUNS + 2}")
            self.assertNotIn("R1", result["run_ids"])
            self.assertTrue(result["truncated"])


if __name__ == "__main__":
    unittest.main()
