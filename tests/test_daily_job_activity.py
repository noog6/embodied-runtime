from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from embodied_runtime.app import ApplicationOptions, JOB_OUTCOME_EVALUATION_REQUEST, RobotApplication
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.interaction import InteractionChannel, OperatorMessageSink
from embodied_runtime.jobs import FilesystemJobWorkspaceStore, JobRunStatus, SQLiteJobStore
from embodied_runtime.jobs.activity import timestamp_us
from embodied_runtime.profile import RobotProfile
from embodied_runtime.run_history import RunHistoryEvidenceReader
from embodied_runtime.temporal_context import LocalDayWindow
from tests.test_platform import snapshot


START = datetime(2026, 11, 6, tzinfo=UTC)
END = datetime(2026, 11, 7, tzinfo=UTC)
RETRIEVED = datetime(2026, 11, 8, 12, tzinfo=UTC)


class DailyActivityStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.store = SQLiteJobStore(self.path, clock=lambda: RETRIEVED)
        self.addCleanup(self.store.close)
        self.job = self.store.create_job("PRIVATE NAME", "PRIVATE DESCRIPTION")

    def row(self, number, status, created, started=None, finished=None, job_id=1):
        # Literal persisted fixtures permit incomplete/legacy fields that the
        # ordinary lifecycle deliberately refuses to construct.
        self.store._connection.execute(
            """INSERT INTO job_runs(id,job_id,status,created_at,started_at,finished_at,
                        outcome_summary,error_summary,result_report) VALUES(?,?,?,?,?,?,?, ?, ?)""",
            (number, job_id, status, created, started, finished,
             "PRIVATE SUMMARY", "PRIVATE ERROR",
             None if status in ("pending", "running") else "PRIVATE REPORT"))

    def summary(self, start=START, end=END, retrieved=RETRIEVED):
        return self.store.summarize_activity(start, end, retrieved_at=retrieved)

    def test_mixed_lifetimes_boundaries_and_later_terminal_outcomes(self):
        self.row(1, "completed", "2026-11-06T01:00Z", "2026-11-06T01:01Z", "2026-11-06T02:00Z")
        self.row(2, "failed", "2026-11-06T03:00Z", "2026-11-06T03:01Z", "2026-11-06T04:00Z")
        self.row(3, "stopped", "2026-11-06T05:00Z", finished="2026-11-06T06:00Z")
        self.row(4, "interrupted", "2026-11-05T20:00Z", finished="2026-11-06T07:00Z")
        self.row(5, "pending", "2026-11-06T08:00Z")
        self.row(6, "running", "2026-11-05T20:00Z", "2026-11-06T09:00Z")
        self.row(7, "completed", "2026-11-06T10:00Z", "2026-11-06T10:01Z", "2026-11-07T01:00Z")
        self.row(8, "completed", "2026-11-05T20:00Z", "2026-11-05T21:00Z", "2026-11-06T12:00Z")
        self.row(9, "completed", "2026-11-05T20:00Z", "2026-11-05T21:00Z", "2026-11-06T00:00Z")
        self.row(10, "completed", "2026-11-06T00:00Z", "2026-11-06T00:00Z", "2026-11-07T00:00Z")
        self.row(11, "completed", "2026-11-07T00:00Z", "2026-11-07T00:00Z", "2026-11-07T00:01Z")
        self.row(12, "completed", "2026-11-05T20:00Z", "2026-11-05T21:00Z", "2026-11-05T23:59:59.999999Z")
        result = self.summary()
        expected = {
            "records": 10, "created": 6, "started": 5,
            "terminal_outcomes": {"completed": 3, "failed": 1, "stopped": 1, "interrupted": 1},
            "statuses_at_retrieval": {"pending": 1, "running": 1, "completed": 5,
                                      "failed": 1, "stopped": 1, "interrupted": 1},
            "open_before_start": 4, "open_before_end": 4,
        }
        self.assertEqual(result["totals"], expected)
        self.assertEqual(result["jobs"], [{"job_id": 1, "counts": expected}])
        self.assertEqual(result["coverage"]["retained_records"], 12)
        self.assertEqual(result["coverage"]["status"], "complete")
        self.assertEqual([run["run_id"] for run in result["runs"]], [2, 3, 4, 5, 6, 7, 10, 1, 8, 9])
        later = next(run for run in result["runs"] if run["run_id"] == 7)
        self.assertEqual(later["status_at_retrieval"], "completed")
        self.assertTrue(later["open_before_end"])
        self.assertFalse(later["in_day"]["terminal"])

    def test_totals_and_job_counts_are_independent_of_detail_caps(self):
        for number in range(2, 16):
            self.store.create_job(f"Private Job {number}")
        for number in range(1, 46):
            self.row(number, "completed", "2026-11-06T01:00Z", "2026-11-06T02:00Z",
                     "2026-11-06T03:00Z", job_id=(number - 1) // 3 + 1)
        result = self.summary()
        self.assertEqual(result["totals"]["records"], 45)
        self.assertEqual(result["totals"]["created"], 45)
        self.assertEqual(result["totals"]["started"], 45)
        self.assertEqual(result["totals"]["terminal_outcomes"]["completed"], 45)
        self.assertEqual(result["coverage"]["status"], "complete")
        self.assertEqual(result["omitted_jobs"], 5)
        self.assertEqual(result["omitted_runs"], 25)
        self.assertEqual([job["job_id"] for job in result["jobs"]], list(range(1, 11)))
        self.assertEqual([run["run_id"] for run in result["runs"]], list(range(1, 21)))
        self.assertTrue(all(job["counts"]["records"] == 3 for job in result["jobs"]))
        self.assertLess(len(json.dumps(result)), 18_000)

    def test_unclassifiable_rows_are_global_uncertainty_not_invented_history(self):
        self.row(1, "running", "2026-11-06T00:00Z")  # missing required start
        self.row(2, "completed", "2026-11-06T00:00Z", "2026-11-06T01:00Z")
        self.row(3, "pending", "2026-11-06T00:00Z", "2026-11-06T01:00Z")
        self.row(4, "pending", "PRIVATE MALFORMED TIMESTAMP")
        self.row(5, "stopped", "2026-11-06T03:00Z", finished="2026-11-06T02:00Z")
        self.row(6, "completed", "2026-11-06T00:00Z", "2026-11-06T01:00Z", "bad")
        self.row(7, "running", "2026-11-06T00:00", "2026-11-06T01:00Z")
        self.row(8, "completed", "2026-11-06T03:00Z", "2026-11-06T02:00Z", "2026-11-06T04:00Z")
        self.row(9, "pending", "2026-11-09T00:00Z")  # beyond retrieval
        self.row(10, "interrupted", "2026-11-06T00:00Z", finished="2026-11-06T01:00Z")
        self.row(11, "running", "2000-01-01T00:00Z", "bad")  # not silently discarded as old
        self.row(12, "running", "2026-11-06T00:00Z", "2026-11-06T01:00Z", "2026-11-06T02:00Z")
        result = self.summary()
        self.assertEqual(result["coverage"], {
            "status": "partial", "retained_records": 12, "unclassified_records": 11,
            "unclassified_day_membership": "unknown"})
        self.assertEqual(result["totals"]["records"], 1)
        self.assertEqual(result["totals"]["started"], 0)
        self.assertEqual(result["totals"]["terminal_outcomes"]["interrupted"], 1)
        self.assertEqual([run["run_id"] for run in result["runs"]], [10])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_later_retrieval_status_does_not_rewrite_boundary_accounting(self):
        self.row(1, "running", "2026-11-06T01:00Z", "2026-11-06T02:00Z")
        first = self.summary()
        self.store.transition_run(1, JobRunStatus.COMPLETED)
        second = self.summary()
        self.assertEqual(first["totals"]["statuses_at_retrieval"]["running"], 1)
        self.assertEqual(second["totals"]["statuses_at_retrieval"]["completed"], 1)
        self.assertEqual(first["totals"]["open_before_end"], 1)
        self.assertEqual(second["totals"]["open_before_end"], 1)
        self.assertEqual(second["totals"]["terminal_outcomes"]["completed"], 0)

    def test_toronto_short_and_long_days_and_exact_microsecond_edges(self):
        cases = (
            ("2026-03-09T02:00:00-04:00", "2026-03-08", "2026-03-08T05:00:00+00:00",
             "2026-03-09T04:00:00+00:00", "2026-03-08T04:59:59.999999Z", "2026-03-09T03:59:59.999999Z", 23),
            ("2026-11-02T02:00:00-05:00", "2026-11-01", "2026-11-01T04:00:00+00:00",
             "2026-11-02T05:00:00+00:00", "2026-11-01T03:59:59.999999Z", "2026-11-02T04:59:59.999999Z", 25),
        )
        for now, day, start, end, before, last, hours in cases:
            with self.subTest(day=day):
                self.store._connection.execute("DELETE FROM job_runs")
                window = LocalDayWindow.previous_day(datetime.fromisoformat(now), "America/Toronto")
                self.assertEqual(window.calendar_date.isoformat(), day)
                self.assertEqual(window.start.isoformat(), start)
                self.assertEqual(window.end.isoformat(), end)
                self.assertEqual((window.end - window.start).total_seconds(), hours * 3600)
                for number, stamp in enumerate((before, start, last, end), 1):
                    self.row(number, "pending", stamp)
                result = self.summary(window.start, window.end, window.retrieved_at)
                self.assertEqual(result["totals"]["records"], 3)
                self.assertEqual(result["totals"]["created"], 2)
                self.assertEqual(result["totals"]["open_before_start"], 1)
                self.assertEqual(result["totals"]["open_before_end"], 3)

    def test_one_read_snapshot_survives_a_concurrent_terminal_update(self):
        self.store._connection.execute("PRAGMA journal_mode=WAL")
        self.row(1, "pending", "2026-11-06T01:00Z")
        writer = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(writer.close)
        changed = False

        def interleaved_timestamp(value):
            nonlocal changed
            if not changed:
                changed = True
                writer.execute("UPDATE job_runs SET status='stopped',finished_at='2026-11-06T02:00Z' WHERE id=1")
            return timestamp_us(value)

        self.store._connection.create_function("job_activity_time", 1, interleaved_timestamp)
        result = self.summary()
        self.assertTrue(changed)
        self.assertEqual(writer.execute("SELECT status FROM job_runs").fetchone()[0], "stopped")
        self.assertEqual(result["totals"]["statuses_at_retrieval"]["pending"], 1)
        self.assertEqual(result["jobs"][0]["counts"]["statuses_at_retrieval"]["pending"], 1)
        self.assertEqual(result["runs"][0]["status_at_retrieval"], "pending")
        self.assertEqual(result["totals"]["terminal_outcomes"]["stopped"], 0)

    def test_empty_read_is_read_only_and_never_selects_authored_columns(self):
        forbidden = {"name", "description", "outcome_summary", "error_summary", "result_report"}

        def authorize(operation, table, column, *_):
            if operation in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                return sqlite3.SQLITE_DENY
            if operation == sqlite3.SQLITE_READ and column in forbidden:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        before = self.store._connection.total_changes
        self.store._connection.set_authorizer(authorize)
        result = self.summary()
        self.assertEqual(result["coverage"]["status"], "complete")
        self.assertEqual(result["coverage"]["retained_records"], 0)
        self.assertEqual(result["totals"]["records"], 0)
        self.assertEqual(result["runs"], [])
        self.assertEqual(self.store._connection.total_changes, before)
        self.assertFalse(self.store._connection.in_transaction)

    def test_offset_timestamps_are_compared_as_instants_and_all_unknown_is_partial(self):
        self.row(1, "completed", "2026-11-05T19:00:00-05:00", "2026-11-05T19:00:00-05:00",
                 "2026-11-06T19:00:00-05:00")
        self.row(2, "pending", "2026-11-06T19:00:00-05:00")
        result = self.summary()
        self.assertEqual(result["totals"]["records"], 1)
        self.assertEqual(result["totals"]["created"], 1)
        self.assertEqual(result["totals"]["started"], 1)
        self.assertEqual(result["totals"]["terminal_outcomes"]["completed"], 0)
        self.assertEqual(result["totals"]["open_before_end"], 1)
        self.store._connection.execute("UPDATE job_runs SET created_at='unreadable'")
        unknown = self.summary()
        self.assertEqual(unknown["coverage"]["status"], "partial")
        self.assertEqual(unknown["coverage"]["unclassified_records"], 2)
        self.assertEqual(unknown["totals"]["records"], 0)

    def test_older_sqlite_reports_unavailable_without_claiming_zero_activity(self):
        with patch("embodied_runtime.jobs.sqlite_store.sqlite3.sqlite_version_info", (3, 34, 1)):
            result = self.summary()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "sqlite_activity_aggregation_unsupported")
        self.assertNotIn("totals", result)


class DailyLogCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reader = RunHistoryEvidenceReader(self.root, "R2", timezone_name="America/Toronto",
            clock=lambda: datetime.fromisoformat("2026-11-07T02:00:00-05:00"))

    def log(self, number, lines):
        directory = self.root / f"R{number}"
        directory.mkdir()
        (directory / "runtime.log").write_text("".join(lines))

    def test_empty_missing_and_unreadable_sources_are_distinct(self):
        empty = self.reader.inspect("overview", "previous_day")
        self.assertEqual(empty["log_coverage"]["status"], "complete")
        self.assertEqual(empty["safe_line_count"], 0)
        (self.root / "R1").mkdir()
        lost = self.reader.inspect("overview", "previous_day")
        self.assertEqual(lost["log_coverage"]["status"], "unavailable")
        self.assertIsNone(lost["safe_line_count"])
        self.assertEqual(lost["log_coverage"]["unavailable_run_ids"], ["R1"])
        missing = RunHistoryEvidenceReader(self.root / "absent").inspect("overview", "previous_day")
        self.assertEqual(missing["log_coverage"]["discovery"], "unavailable")
        self.assertIsNone(missing["log_coverage"]["discovered_directories"])
        with patch.object(self.reader, "_discover_safe_ids", side_effect=PermissionError):
            denied = self.reader.inspect("overview", "previous_day")
        self.assertEqual(denied["log_coverage"]["status"], "unavailable")
        (self.root / "R1" / "runtime.log").write_text("")
        empty_log = self.reader.inspect("overview", "previous_day")
        self.assertEqual(empty_log["log_coverage"]["status"], "complete")
        self.assertEqual(empty_log["log_coverage"]["readable_logs"], 1)
        self.assertEqual(empty_log["safe_line_count"], 0)

    def test_multiple_sessions_active_runtime_privacy_and_partial_source(self):
        self.log(1, ["2026-11-06T05:00:00.000Z [APP] status=started\n"])
        self.log(2, ["2026-11-07T04:59:59.000Z [JOBS] work=completed disposition=completed\n",
                     "2026-11-07T04:59:59.001Z [VOICE] text=PRIVATE\n",
                     "2026-11-07T05:00:00.000Z [APP] status=next_day\n",
                     "2026-11-07T05:01:00.000Z [APP] incomplete"])
        # No final process summary is present or needed for either session.
        (self.root / "R3").mkdir()
        result = self.reader.inspect("overview", "previous_day")
        self.assertEqual(result["run_ids"], ["R1", "R2"])
        self.assertEqual(result["safe_line_count"], 2)
        self.assertEqual(result["log_coverage"]["status"], "partial")
        self.assertEqual(result["log_coverage"]["readable_logs"], 2)
        self.assertEqual(result["log_coverage"]["unavailable_run_ids"], ["R3"])
        self.assertEqual(result["log_details"]["omitted_lines"], 0)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_directory_cap_and_excerpt_omission_are_independent(self):
        line = "2026-11-06T12:00:00.000Z [APP] status=ready\n"
        for number in range(1, 23):
            self.log(number, [line])
        capped = self.reader.inspect("overview", "previous_day")
        self.assertEqual(capped["log_coverage"]["status"], "partial")
        self.assertEqual(capped["log_coverage"]["omitted_directories"], 2)
        self.assertEqual(capped["log_coverage"]["examined_directories"], 20)
        self.assertEqual(capped["log_details"]["omitted_lines"], 0)
        with tempfile.TemporaryDirectory() as other:
            root = Path(other)
            (root / "R1").mkdir()
            (root / "R1" / "runtime.log").write_text(line * 50)
            excerpts = RunHistoryEvidenceReader(root, clock=self.reader._clock).inspect("overview", "previous_day")
            self.assertEqual(excerpts["log_coverage"]["status"], "complete")
            self.assertEqual(excerpts["log_details"], {"returned_lines": 24, "omitted_lines": 26, "clipped_lines": 0})
            search = RunHistoryEvidenceReader(root, clock=self.reader._clock).inspect("search", "previous_day", "READY")
            self.assertEqual(search["log_details"]["returned_lines"], 20)
            self.assertEqual(search["log_details"]["omitted_lines"], 30)

    def test_mid_read_failure_keeps_partial_evidence_and_source_identity(self):
        (self.root / "R1").mkdir()

        def partial(_):
            yield 1, "2026-11-06T12:00:00.000Z [APP] status=ready"
            raise OSError("PRIVATE PATH")

        with patch.object(self.reader, "_complete_lines", partial):
            result = self.reader.inspect("overview", "previous_day")
        self.assertEqual(result["safe_line_count"], 1)
        self.assertEqual(result["run_ids"], ["R1"])
        self.assertEqual(result["log_coverage"]["status"], "partial")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_safe_lines_with_invalid_dates_report_unknown_day_membership(self):
        self.log(1, ["2026-13-06T12:00:00.000Z [APP] status=ready\n",
                     "2026-13-06T12:00:00.000Z [VOICE] text=PRIVATE\n"])
        result = self.reader.inspect("overview", "previous_day")
        self.assertEqual(result["log_coverage"]["unclassified_timestamp_lines"], 1)
        self.assertEqual(result["log_coverage"]["status"], "partial")
        self.assertEqual(result["safe_line_count"], 0)
        self.assertNotIn("PRIVATE", json.dumps(result))


class Platform:
    def snapshot(self):
        return snapshot()


class RecordingSink(OperatorMessageSink):
    channel = InteractionChannel.CONSOLE

    def __init__(self):
        self.messages = []

    async def deliver(self, message):
        self.messages.append(message)


class ReviewBackend(TextCognitionBackend):
    identifier = "offline-daily-review"

    def __init__(self):
        self.tools = []
        self.instructions = []
        self.results = []
        self.actions = [
            ("inspect_run_history", {"selector": "previous_day", "query": None}),
            ("inspect_run_history", {"selector": "previous_day", "query": "run=RUN1 "}),
            ("workspace_write", {"path": "reports/day.md", "mode": "create", "content": "Bounded assessment; recorded completion does not prove useful output."}),
        ]

    async def respond(self, message, *, instructions=None, tools=(), tool_executor=None, **_):
        self.tools.append(tuple(tool.name for tool in tools))
        self.instructions.append(instructions)
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            action = ("report_job_outcome", {"disposition": "completed", "summary": "Review retained",
                "report": "Bounded review at reports/day.md", "readiness": None, "delay_seconds": None})
        else:
            action = self.actions.pop(0)
        self.results.append(json.loads((await tool_executor(CognitionToolCall(action[0], json.dumps(action[1])))).output))
        return "bounded work"


class DailyOverviewApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime.fromisoformat("2026-11-07T02:00:00-05:00")
        self.store = SQLiteJobStore(self.root / "jobs.sqlite3", clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.workspaces = FilesystemJobWorkspaceStore(self.root / "workspaces")
        self.addCleanup(self.workspaces.close)
        self.source = self.store.create_job("PRIVATE NAME", "PRIVATE DESCRIPTION")
        self.store._connection.execute("""INSERT INTO job_runs(id,job_id,status,created_at,started_at,
            finished_at,outcome_summary,error_summary,result_report) VALUES(1,1,'completed',
            '2026-11-06T06:00Z','2026-11-06T06:01Z','2026-11-06T06:02Z',
            'PRIVATE SUMMARY','PRIVATE ERROR','PRIVATE REPORT')""")
        history_root = self.root / "runs"
        history_root.mkdir()
        (history_root / "R1").mkdir()
        (history_root / "R1" / "runtime.log").write_text(
            "2026-11-06T06:02:00.000Z [JOBS] run=RUN1 work=completed disposition=completed\n")
        self.reader = RunHistoryEvidenceReader(history_root, "R1")
        self.apps = []
        self.sink = RecordingSink()

    async def asyncTearDown(self):
        for app in self.apps:
            await app.stop()

    async def app(self, backend=None, *, jobs=True, initiative=True):
        app = RobotApplication(RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=initiative, shared_reads_enabled=False,
                               initiative_messages_enabled=True),
            platform_provider=Platform(), cognition_backend=backend,
            timezone_name="America/Toronto", wall_clock=lambda: self.now,
            run_history_evidence=self.reader, job_store=self.store if jobs else None,
            job_workspace_store=self.workspaces, operator_message_sink=self.sink)
        self.apps.append(app)
        await app.start()
        return app

    def inspect(self, app, **kwargs):
        return json.loads(app._execute_run_history_inspection(CognitionToolCall(
            "inspect_run_history", '{"selector":"previous_day","query":null}'), **kwargs).output)

    async def test_autonomous_two_reads_write_completion_and_metadata_privacy(self):
        self.store._connection.executemany("""INSERT INTO job_runs(job_id,status,created_at,
            started_at,finished_at) VALUES(1,'completed','2026-11-06T06:00Z',
            '2026-11-06T06:01Z','2026-11-06T06:02Z')""", [()] * 44)
        backend = ReviewBackend()
        app = await self.app(backend)
        reviewer = self.store.create_job("Any reviewer", "A bounded operational assessment")
        binding = app.start_job_run(reviewer.id)
        with patch.object(self.workspaces, "write", wraps=self.workspaces.write) as write:
            outcome = await app.work_current_job_once()
        self.assertEqual(outcome.disposition.value, "completed")
        self.assertEqual(write.call_count, 1)
        self.assertEqual(backend.results[0]["job_activity"]["totals"]["records"], 45)
        self.assertEqual(backend.results[0]["job_activity"]["totals"]["terminal_outcomes"]["completed"], 45)
        self.assertEqual(backend.results[0]["job_activity"]["omitted_runs"], 25)
        self.assertIn("job_activity", backend.instructions[1])
        self.assertEqual(backend.results[2]["status"], "applied")
        self.assertTrue(backend.results[2]["durability_confirmed"])
        self.assertEqual(len(backend.tools), 4)
        self.assertIn("inspect_run_history", backend.tools[0])
        self.assertIn("inspect_run_history", backend.tools[1])
        self.assertNotIn("inspect_run_history", backend.tools[2])
        self.assertNotIn("shared_inspect_job_run", backend.tools[0])
        self.assertNotIn("discover_jobs", backend.tools[0])
        self.assertIn("address_operator", backend.tools[0])
        self.assertEqual(self.sink.messages, [])
        self.assertFalse(app.options.shared_reads_enabled)
        self.assertIsNone(app.current_job_run)
        self.assertEqual(self.store.get_run(binding.run.id).status, JobRunStatus.COMPLETED)
        self.assertEqual(self.workspaces.read(reviewer.id, "reports/day.md").content,
                         "Bounded assessment; recorded completion does not prove useful output.")
        self.assertNotIn("PRIVATE", json.dumps(backend.results[0]))

    async def test_day_is_resolved_once_even_when_clock_crosses_midnight(self):
        app = await self.app()
        before = datetime.fromisoformat("2026-11-07T23:59:59-05:00")
        after = datetime.fromisoformat("2026-11-08T00:00:01-05:00")
        app._wall_clock = Mock(side_effect=[before, after])
        # Reader's own default UTC clock/timezone must not reselect the day.
        with patch.object(self.reader, "_clock", side_effect=AssertionError("second clock")):
            result = self.inspect(app)
        app._wall_clock.assert_called_once_with()
        self.assertEqual(result["calendar_date"], "2026-11-06")
        self.assertEqual(result["utc_start"], "2026-11-06T05:00:00+00:00")
        self.assertEqual(result["utc_end"], "2026-11-07T05:00:00+00:00")
        for field in ("calendar_date", "timezone", "utc_start", "utc_end", "retrieved_at"):
            self.assertEqual(result[field], result["job_activity"][field])
        self.assertEqual(result["safe_line_count"], 1)
        self.assertEqual(result["job_activity"]["totals"]["records"], 1)

    async def test_log_and_database_availability_are_independent(self):
        app = await self.app()
        with patch.object(self.store, "summarize_activity", side_effect=sqlite3.OperationalError("PRIVATE ERROR")):
            result = self.inspect(app)
        self.assertEqual(result["job_activity"]["status"], "unavailable")
        self.assertNotIn("totals", result["job_activity"])
        self.assertEqual(result["safe_line_count"], 1)
        self.assertEqual(result["log_coverage"]["status"], "complete")
        self.assertNotIn("PRIVATE", json.dumps(result))
        (self.root / "runs" / "R1" / "runtime.log").unlink()
        result = self.inspect(app)
        self.assertEqual(result["log_coverage"]["status"], "unavailable")
        self.assertIsNone(result["safe_line_count"])
        self.assertEqual(result["job_activity"]["coverage"]["status"], "complete")
        self.assertEqual(result["job_activity"]["totals"]["records"], 1)
        app_without_jobs = await self.app(jobs=False)
        self.assertEqual(self.inspect(app_without_jobs)["job_activity"]["reason"], "jobs_persistence_unavailable")

    async def test_stale_autonomous_goal_cannot_read_summary(self):
        app = await self.app()
        app.set_goal("Review evidence")
        goal = app.active_goal
        self.assertIn("inspect_run_history", [tool.name for tool in app.acquisition_tools()])
        app.resolve_goal("cancelled")
        with patch.object(self.store, "summarize_activity") as summary:
            self.assertEqual(self.inspect(app, autonomous=True, expected_goal=goal)["status"], "rejected")
        summary.assert_not_called()
        self.assertNotIn("inspect_run_history", [tool.name for tool in app.acquisition_tools()])

    async def test_terminal_job_binding_and_experiment_projection_do_not_gain_authority(self):
        app = await self.app()
        reviewer = self.store.create_job("Reviewer")
        app.start_job_run(reviewer.id)
        goal = app.active_goal
        app.finish_job_run(JobRunStatus.STOPPED)
        with patch.object(self.store, "summarize_activity") as summary:
            self.assertEqual(self.inspect(app, autonomous=True, expected_goal=goal)["status"], "rejected")
        summary.assert_not_called()
        experiment = self.store.create_procedure_experiment()
        self.store.set_job_enabled(experiment.id, True)
        app.start_job_run(experiment.id)
        self.assertEqual([tool.name for tool in app._acquisition_tools_for_episode(job_work=True)], ["inspect_self"])

    async def test_overview_telemetry_uses_counts_not_search_matches(self):
        app = await self.app()
        with self.assertLogs("embodied_runtime.app", level="INFO") as logs:
            self.inspect(app)
        telemetry = next(line for line in logs.output if "[HISTORY]" in line)
        self.assertIn("returned_lines=1 safe_lines=1", telemetry)
        self.assertIn("jobs_records=1 jobs_coverage=complete", telemetry)
        self.assertNotIn("matches=", telemetry)
        self.assertNotIn("PRIVATE", telemetry)
