"""SQLite-backed durable Job catalog."""

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import unicodedata
from zoneinfo import ZoneInfo

from .focus import JobFocus, JobFocusProposal
from .procedure import (ProcedureProposal, ProcedureFeedback, MAX_PROCEDURE_RUNS,
                        EXPERIMENT_DESCRIPTION, render_procedure)
from .model import (
    Finding, FindingEvidence, FindingEvidenceClass, FindingKind,
    MAX_FINDING_QUERY_CHARS, MAX_FINDING_SEARCH_LIMIT,
    InvalidJobRunTransitionError, Job, JobRun, JobRunStatus, JobSchedule, JobTarget,
    JobTrigger, JobTriggerType,
    RUN_TRANSITIONS, TERMINAL_RUN_STATUSES,
)

SCHEMA_VERSION = 12
_SCHEDULES_SCHEMA = """CREATE TABLE job_schedules (
       job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE RESTRICT,
       enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
       local_time TEXT, timezone TEXT NOT NULL,
       last_started_local_date TEXT,
       cadence TEXT NOT NULL DEFAULT 'daily' CHECK(cadence IN ('daily','hourly')),
       minute INTEGER CHECK(minute BETWEEN 0 AND 59),
       CHECK((cadence='daily' AND local_time IS NOT NULL AND minute IS NULL) OR
             (cadence='hourly' AND local_time IS NULL AND minute IS NOT NULL)))"""
_HOURLY_ACTIVATION_SCHEMA = """CREATE TABLE IF NOT EXISTS job_hourly_activation (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE RESTRICT,
    last_started_slot TEXT NOT NULL)"""
_FOCUS_SCHEMA = """CREATE TABLE job_focus (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE RESTRICT,
    question TEXT NOT NULL CHECK(length(question) BETWEEN 1 AND 500),
    status TEXT NOT NULL CHECK(status IN ('open','blocked')),
    blocker TEXT CHECK(blocker IS NULL OR length(blocker) BETWEEN 1 AND 300),
    source_run_id INTEGER NOT NULL REFERENCES job_runs(id) ON DELETE RESTRICT,
    author TEXT NOT NULL CHECK(author IN ('model','operator')),
    source_task_id TEXT, source_episode_id INTEGER, basis TEXT,
    source_finished_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    version INTEGER NOT NULL, definition_identity TEXT NOT NULL)"""
_FINDINGS_SCHEMA = """CREATE TABLE IF NOT EXISTS job_findings (
       id INTEGER PRIMARY KEY,
       job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
       run_id INTEGER NOT NULL REFERENCES job_runs(id) ON DELETE RESTRICT,
       task_id TEXT NOT NULL, episode_id INTEGER NOT NULL,
       topic TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('observation','synthesis')),
       claim TEXT NOT NULL, evidence_json TEXT NOT NULL, published_at TEXT NOT NULL)"""
_FINDINGS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_job_findings_job ON job_findings(job_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_job_findings_run ON job_findings(run_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_job_findings_published ON job_findings(published_at DESC, id DESC)",
)
_JOB_RUNS_SCHEMA = """CREATE TABLE job_runs (
       id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
       status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','stopped','interrupted')),
       created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
       outcome_summary TEXT, error_summary TEXT, result_report TEXT)"""
_PROCEDURE_SCHEMA = (
    """CREATE TABLE job_procedures (
        job_id INTEGER NOT NULL REFERENCES jobs(id), version INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('active','pending')),
        record_json TEXT NOT NULL CHECK(length(record_json)<=24000),
        PRIMARY KEY(job_id,version), UNIQUE(job_id,state))""",
    """CREATE TABLE job_run_procedures (
        run_id INTEGER PRIMARY KEY REFERENCES job_runs(id),
        job_id INTEGER NOT NULL REFERENCES jobs(id), definition_identity TEXT NOT NULL,
        snapshot_json TEXT CHECK(snapshot_json IS NULL OR length(snapshot_json)<=24000),
        supplied INTEGER NOT NULL DEFAULT 0 CHECK(supplied IN (0,1)),
        feedback_json TEXT CHECK(feedback_json IS NULL OR length(feedback_json)<=8000),
        candidate_disposition TEXT CHECK(candidate_disposition IS NULL OR candidate_disposition IN ('proposed','pending_exists')))""",
    "CREATE INDEX idx_job_run_procedures_job ON job_run_procedures(job_id,run_id)",
)
_SCHEMA = (
    """CREATE TABLE jobs (
       id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
       enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
       target_kind TEXT, target_identifier TEXT,
       created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
       carry_forward_enabled INTEGER NOT NULL DEFAULT 0 CHECK(carry_forward_enabled IN (0,1)),
       focus_version INTEGER NOT NULL DEFAULT 0,
       procedure_enabled INTEGER NOT NULL DEFAULT 0 CHECK(procedure_enabled IN (0,1)),
       procedure_version INTEGER NOT NULL DEFAULT 0,
       procedure_experiment INTEGER NOT NULL DEFAULT 0 CHECK(procedure_experiment IN (0,1)),
       CHECK((target_kind IS NULL AND target_identifier IS NULL) OR
             (target_kind IS NOT NULL AND target_identifier IS NOT NULL)))""",
    _JOB_RUNS_SCHEMA,
    "CREATE INDEX idx_job_runs_job ON job_runs(job_id, id)",
    _SCHEDULES_SCHEMA,
    """CREATE TABLE IF NOT EXISTS job_triggers (
       job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
       event_type TEXT NOT NULL CHECK(event_type IN ('power_attention_required',
           'thermal_warning_raised','memory_pressure_raised','runtime_ready')),
       enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
       PRIMARY KEY(job_id,event_type))""",
    _FINDINGS_SCHEMA,
    *_FINDINGS_INDEXES,
    _FOCUS_SCHEMA,
    _HOURLY_ACTIVATION_SCHEMA,
    *_PROCEDURE_SCHEMA,
)


class SQLiteJobStore:
    """A process-local connection to one globally visible Job catalog."""

    def __init__(self, path: str | Path, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 timeout: float = 5.0) -> None:
        self._clock = clock
        self._connection = sqlite3.connect(path, timeout=timeout, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        try:
            self._initialize_schema()
        except BaseException:
            self._connection.close()
            raise

    def _initialize_schema(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version == 11:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute("ALTER TABLE jobs ADD COLUMN procedure_enabled INTEGER NOT NULL DEFAULT 0 CHECK(procedure_enabled IN (0,1))")
                self._connection.execute("ALTER TABLE jobs ADD COLUMN procedure_version INTEGER NOT NULL DEFAULT 0")
                self._connection.execute("ALTER TABLE jobs ADD COLUMN procedure_experiment INTEGER NOT NULL DEFAULT 0 CHECK(procedure_experiment IN (0,1))")
                for statement in _PROCEDURE_SCHEMA:
                    self._connection.execute(statement)
                self._connection.execute("PRAGMA user_version=12")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            return
        if version == 10:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute("ALTER TABLE job_schedules RENAME TO job_schedules_v10")
                self._connection.execute(_SCHEDULES_SCHEMA)
                self._connection.execute("""INSERT INTO job_schedules
                    (job_id,enabled,local_time,timezone,last_started_local_date)
                    SELECT job_id,enabled,local_time,timezone,last_started_local_date
                    FROM job_schedules_v10""")
                self._connection.execute("DROP TABLE job_schedules_v10")
                self._connection.execute(_HOURLY_ACTIVATION_SCHEMA)
                self._connection.execute("PRAGMA user_version = 11")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            self._initialize_schema()
            return
        if version == 9:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute("ALTER TABLE jobs ADD COLUMN carry_forward_enabled INTEGER NOT NULL DEFAULT 0 CHECK(carry_forward_enabled IN (0,1))")
                self._connection.execute("ALTER TABLE jobs ADD COLUMN focus_version INTEGER NOT NULL DEFAULT 0")
                self._connection.execute(_FOCUS_SCHEMA)
                self._connection.execute("PRAGMA user_version = 10")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            self._initialize_schema()
            return
        if version == 8:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(_FINDINGS_SCHEMA)
                for statement in _FINDINGS_INDEXES:
                    self._connection.execute(statement)
                self._connection.execute("PRAGMA user_version = 9")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            self._initialize_schema()
            return
        if version == 7:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "DROP INDEX IF EXISTS idx_job_triggers_enabled_event")
                self._connection.execute("PRAGMA user_version = 8")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            self._initialize_schema()
            return
        if version in (5, 6):
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute("DROP INDEX IF EXISTS idx_job_triggers_enabled_event")
                self._connection.execute("ALTER TABLE job_triggers RENAME TO job_triggers_previous")
                self._connection.execute(_SCHEMA[4])
                self._connection.execute(
                    """INSERT INTO job_triggers(job_id,event_type,enabled)
                       SELECT job_id,event_type,enabled FROM job_triggers_previous""")
                self._connection.execute("DROP TABLE job_triggers_previous")
                self._connection.execute("PRAGMA user_version = 8")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            self._initialize_schema()
            return
        if version == 4:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(_SCHEMA[4])
                self._connection.execute("PRAGMA user_version = 8")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            self._initialize_schema()
            return
        if version in (1, 2, 3):
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if version == 1:
                    self._connection.execute(_SCHEMA[3])
                if version in (1, 2):
                    self._connection.execute("ALTER TABLE job_runs ADD COLUMN result_report TEXT")
                self._connection.execute("ALTER TABLE job_runs RENAME TO job_runs_v3")
                self._connection.execute(_JOB_RUNS_SCHEMA)
                self._connection.execute(
                    """INSERT INTO job_runs
                       (id,job_id,status,created_at,started_at,finished_at,
                        outcome_summary,error_summary,result_report)
                       SELECT id,job_id,status,created_at,started_at,finished_at,
                              outcome_summary,error_summary,result_report
                       FROM job_runs_v3"""
                )
                self._connection.execute("DROP TABLE job_runs_v3")
                self._connection.execute("CREATE INDEX idx_job_runs_job ON job_runs(job_id, id)")
                self._connection.execute(_SCHEMA[4])
                self._connection.execute("PRAGMA user_version = 8")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            self._initialize_schema()
            return
        if version != 0:
            raise RuntimeError(f"unsupported jobs schema version {version}; expected {SCHEMA_VERSION}")
        existing = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if existing is not None:
            raise RuntimeError("unversioned non-empty jobs database")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA:
                self._connection.execute(statement)
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("job store clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def create_job(self, name: str, description: str = "", *, enabled: bool = True,
                   target: JobTarget | None = None) -> Job:
        now = self._now()
        # Construct a validation probe before writing; the generated ID is replaced below.
        probe = Job(1, name, description, enabled, target, now, now)
        cursor = self._connection.execute(
            """INSERT INTO jobs (name, description, enabled, target_kind,
               target_identifier, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (probe.name, probe.description, int(enabled), target.kind if target else None,
             target.identifier if target else None, _format(now), _format(now)),
        )
        return Job(cursor.lastrowid, probe.name, probe.description, enabled, target, now, now)

    def get_job(self, job_id: int) -> Job | None:
        _id(job_id, "job")
        row = self._connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row) if row is not None else None

    def list_jobs(self, *, target: JobTarget | None = None) -> tuple[Job, ...]:
        if target is None:
            rows = self._connection.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        else:
            if not isinstance(target, JobTarget):
                raise TypeError("target must be a JobTarget or None")
            rows = self._connection.execute(
                "SELECT * FROM jobs WHERE target_kind = ? AND target_identifier = ? ORDER BY id",
                (target.kind, target.identifier),
            ).fetchall()
        return tuple(_job(row) for row in rows)

    def discover_jobs(self, *, after_id: int = 0, limit: int = 5) -> tuple[Job, ...]:
        """Bounded catalog page, including disabled Jobs, in stable numeric ID order."""
        if type(after_id) is not int or not 0 <= after_id <= 2**63 - 1:
            raise ValueError("invalid catalog cursor")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("invalid catalog limit")
        rows = self._connection.execute(
            "SELECT * FROM jobs WHERE id > ? ORDER BY id LIMIT ?",
            (after_id, limit),
        ).fetchall()
        return tuple(_job(row) for row in rows)

    def set_job_enabled(self, job_id: int, enabled: bool) -> Job:
        _id(job_id, "job")
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be boolean")
        now = self._now()
        cursor = self._connection.execute(
            "UPDATE jobs SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), _format(now), job_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown job: {job_id}")
        return self.get_job(job_id)  # type: ignore[return-value]

    def set_job_description(self, job_id: int, description: str) -> Job:
        """Replace only a Job's normalized description and modification time."""
        _id(job_id, "job")
        current = self.get_job(job_id)
        if current is None:
            raise KeyError(f"unknown job: {job_id}")
        now = self._now()
        replacement = Job(
            current.id, current.name, description, current.enabled, current.target,
            current.created_at, now, current.carry_forward_enabled,
        )
        cursor = self._connection.execute(
            "UPDATE jobs SET description = ?, updated_at = ? WHERE id = ?",
            (replacement.description, _format(replacement.updated_at), job_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown job: {job_id}")
        return replacement

    def create_run(self, job_id: int, *, require_idle: bool = False) -> JobRun:
        _id(job_id, "job")
        if require_idle:
            run = self.create_triggered_run(job_id)
            if run is None:
                raise RuntimeError(f"JOB{job_id} is disabled or already has an active occurrence")
            return run
        now = self._now()
        try:
            cursor = self._connection.execute(
                "INSERT INTO job_runs (job_id, status, created_at) VALUES (?, 'pending', ?)",
                (job_id, _format(now)),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"job does not exist: {job_id}") from error
        return JobRun(cursor.lastrowid, job_id, JobRunStatus.PENDING, now)

    def set_schedule(self, job_id: int, local_time: str | None, timezone: str, *,
                     enabled: bool = True, cadence: str = "daily",
                     minute: int | None = None) -> JobSchedule:
        schedule = JobSchedule(job_id, enabled, local_time, timezone,
                               cadence=cadence, minute=minute)
        if self.get_job(job_id) is None:
            raise KeyError(f"unknown job: {job_id}")
        self._connection.execute(
            """INSERT INTO job_schedules(job_id, enabled, local_time, timezone,cadence,minute)
               VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET
               enabled=excluded.enabled, local_time=excluded.local_time,
               timezone=excluded.timezone,cadence=excluded.cadence,minute=excluded.minute""",
            (job_id, int(enabled), local_time, timezone, cadence, schedule.minute),
        )
        return self.get_schedule(job_id)  # type: ignore[return-value]

    def get_schedule(self, job_id: int) -> JobSchedule | None:
        _id(job_id, "job")
        row = self._connection.execute(
            """SELECT s.*, a.last_started_slot FROM job_schedules s
               LEFT JOIN job_hourly_activation a ON a.job_id=s.job_id WHERE s.job_id=?""", (job_id,)
        ).fetchone()
        return _schedule(row) if row is not None else None

    def list_schedules(self) -> tuple[JobSchedule, ...]:
        rows = self._connection.execute("""SELECT s.*, a.last_started_slot FROM job_schedules s
            LEFT JOIN job_hourly_activation a ON a.job_id=s.job_id ORDER BY s.job_id""").fetchall()
        return tuple(_schedule(row) for row in rows)

    def remove_schedule(self, job_id: int) -> bool:
        _id(job_id, "job")
        return self._connection.execute(
            "DELETE FROM job_schedules WHERE job_id=?", (job_id,)
        ).rowcount == 1

    def set_trigger(self, job_id: int, event_type: JobTriggerType, *,
                    enabled: bool = True) -> JobTrigger:
        trigger = JobTrigger(job_id, enabled, event_type)
        if self.get_job(job_id) is None:
            raise KeyError(f"unknown job: {job_id}")
        self._connection.execute(
            """INSERT INTO job_triggers(job_id,event_type,enabled) VALUES(?,?,?)
               ON CONFLICT(job_id,event_type) DO UPDATE SET enabled=excluded.enabled""",
            (job_id, event_type.value, int(enabled)))
        return trigger

    def get_trigger(self, job_id: int, event_type: JobTriggerType) -> JobTrigger | None:
        JobTrigger(job_id, True, event_type)
        row = self._connection.execute(
            "SELECT * FROM job_triggers WHERE job_id=? AND event_type=?",
            (job_id, event_type.value)).fetchone()
        return None if row is None else JobTrigger(row["job_id"], bool(row["enabled"]),
                                                    JobTriggerType(row["event_type"]))

    def list_triggers(self, event_type: JobTriggerType | None = None) -> tuple[JobTrigger, ...]:
        if event_type is not None and not isinstance(event_type, JobTriggerType):
            raise TypeError("event_type must be a JobTriggerType or None")
        rows = self._connection.execute(
            "SELECT * FROM job_triggers" + (" WHERE event_type=?" if event_type else "") +
            " ORDER BY job_id,event_type", (() if event_type is None else (event_type.value,))
        ).fetchall()
        return tuple(JobTrigger(row["job_id"], bool(row["enabled"]),
                                JobTriggerType(row["event_type"])) for row in rows)

    def remove_trigger(self, job_id: int, event_type: JobTriggerType) -> bool:
        JobTrigger(job_id, True, event_type)
        return self._connection.execute(
            "DELETE FROM job_triggers WHERE job_id=? AND event_type=?",
            (job_id, event_type.value)).rowcount == 1

    def create_triggered_run(self, job_id: int) -> JobRun | None:
        """Atomically admit one event occurrence only when no active run exists."""
        _id(job_id, "job")
        now = self._now()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            job = self.get_job(job_id)
            if job is None or not job.enabled:
                self._connection.rollback()
                return None
            active = self._connection.execute(
                "SELECT id FROM job_runs WHERE job_id=? AND status IN ('pending','running') LIMIT 1",
                (job_id,)).fetchone()
            if active is not None:
                self._connection.rollback()
                return None
            cursor = self._connection.execute(
                "INSERT INTO job_runs(job_id,status,created_at) VALUES(?,'pending',?)",
                (job_id, _format(now)))
            self._connection.commit()
            return JobRun(cursor.lastrowid, job_id, JobRunStatus.PENDING, now)
        except BaseException:
            self._connection.rollback()
            raise

    def create_scheduled_run(self, job_id: int, slot: str | datetime, *,
                             expected_schedule: JobSchedule | None = None,
                             clock: Callable[[], datetime] | None = None) -> JobRun | None:
        """Revalidate, consume a slot, and create a nonoverlapping run atomically.

        Date strings preserve the daily store API. Runtime admission supplies an
        instant, its schedule snapshot, and the application clock for revalidation
        after acquiring the write lock, which may have crossed a slot boundary.
        """
        _id(job_id, "job")
        created_at = self._now()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            schedule = self.get_schedule(job_id)
            job = self.get_job(job_id)
            active = self._connection.execute(
                "SELECT id FROM job_runs WHERE job_id=? AND status IN ('pending','running') LIMIT 1",
                (job_id,)).fetchone()
            if (schedule is None or not schedule.enabled or job is None or not job.enabled
                    or active is not None
                    or (expected_schedule is not None and schedule != expected_schedule)):
                self._connection.rollback()
                return None
            if isinstance(slot, str):
                JobSchedule(job_id, True, "00:00", "UTC", slot)
                if schedule.cadence != "daily" or schedule.last_started_local_date == slot:
                    self._connection.rollback()
                    return None
                marker, value = "last_started_local_date", slot
            else:
                if slot.tzinfo is None or slot.utcoffset() is None:
                    raise ValueError("scheduled slot must be timezone-aware")
                slot = slot.astimezone(UTC)
                if schedule.due_slot((clock or self._now)()) != slot:
                    self._connection.rollback()
                    return None
                if schedule.cadence == "hourly":
                    marker, value = "last_started_slot", _format(slot)
                else:
                    marker, value = "last_started_local_date", slot.astimezone(
                        ZoneInfo(schedule.timezone)).date().isoformat()
            if marker == "last_started_local_date":
                self._connection.execute(
                    "UPDATE job_schedules SET last_started_local_date=? WHERE job_id=?", (value, job_id))
            cursor = self._connection.execute(
                "INSERT INTO job_runs(job_id,status,created_at) VALUES(?,'pending',?)",
                (job_id, _format(created_at)),
            )
            run = JobRun(cursor.lastrowid, job_id, JobRunStatus.PENDING, created_at)
            if marker == "last_started_slot":
                self._connection.execute("""INSERT INTO job_hourly_activation(job_id,last_started_slot)
                    VALUES(?,?) ON CONFLICT(job_id) DO UPDATE SET last_started_slot=excluded.last_started_slot""",
                    (job_id, value))
            self._connection.commit()
            return run
        except BaseException:
            self._connection.rollback()
            raise

    def get_run(self, run_id: int) -> JobRun | None:
        _id(run_id, "job run")
        row = self._connection.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
        return _run(row) if row is not None else None

    def list_runs(self, job_id: int) -> tuple[JobRun, ...]:
        _id(job_id, "job")
        rows = self._connection.execute(
            "SELECT * FROM job_runs WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        return tuple(_run(row) for row in rows)

    def get_latest_completed_run(self, job_id: int) -> JobRun | None:
        """Return the newest completed occurrence, excluding every other state."""
        _id(job_id, "job")
        row = self._connection.execute(
            """SELECT * FROM job_runs WHERE job_id = ? AND status = 'completed'
               ORDER BY id DESC LIMIT 1""", (job_id,)
        ).fetchone()
        return _run(row) if row is not None else None

    def interrupt_nonterminal_runs(self) -> tuple[JobRun, ...]:
        """Atomically terminalize occurrences abandoned by an earlier process."""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self._connection.execute(
                """SELECT * FROM job_runs WHERE status IN ('pending','running')
                   ORDER BY id"""
            ).fetchall()
            if not rows:
                self._connection.commit()
                return ()
            now = self._now()
            formatted = _format(now)
            cursor = self._connection.execute(
                """UPDATE job_runs SET status='interrupted', finished_at=?
                   WHERE status IN ('pending','running')""",
                (formatted,),
            )
            if cursor.rowcount != len(rows):
                raise InvalidJobRunTransitionError("job runs changed concurrently")
            interrupted = tuple(
                JobRun(
                    row["id"], row["job_id"], JobRunStatus.INTERRUPTED,
                    _parse(row["created_at"]), _parse(row["started_at"]), now,
                    row["outcome_summary"], row["error_summary"], row["result_report"],
                )
                for row in rows
            )
            self._connection.commit()
            return interrupted
        except BaseException:
            self._connection.rollback()
            raise

    def transition_run(self, run_id: int, status: JobRunStatus, *,
                       outcome_summary: str | None = None,
                       error_summary: str | None = None,
                       result_report: str | None = None,
                       focus_update: JobFocusProposal | None = None,
                       focus_definition: str | None = None,
                       focus_job_id: int | None = None,
                       focus_task_id: str | None = None,
                       focus_episode_id: int | None = None,
                       procedure_candidate: ProcedureProposal | None = None,
                       procedure_feedback: ProcedureFeedback | None = None,
                       procedure_definition: str | None = None,
                       procedure_job_id: int | None = None,
                       procedure_task_id: str | None = None,
                       procedure_episode_id: int | None = None) -> JobRun:
        _id(run_id, "job run")
        if not isinstance(status, JobRunStatus):
            raise TypeError("status must be a JobRunStatus")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.get_run(run_id)
            if current is None:
                raise KeyError(f"unknown job run: {run_id}")
            if status not in RUN_TRANSITIONS[current.status]:
                raise InvalidJobRunTransitionError(
                    f"job run cannot transition from {current.status.value} to {status.value}"
                )
            now = self._now()
            started = now if status is JobRunStatus.RUNNING else current.started_at
            finished = now if status in TERMINAL_RUN_STATUSES else None
            # Validate summaries and timestamp invariants before updating.
            next_run = JobRun(current.id, current.job_id, status, current.created_at,
                              started, finished, outcome_summary, error_summary,
                              result_report)
            cursor = self._connection.execute(
                """UPDATE job_runs SET status=?, started_at=?, finished_at=?,
                   outcome_summary=?, error_summary=?, result_report=?
                   WHERE id=? AND status=?""",
                (status.value, _format(started) if started else None,
                 _format(finished) if finished else None, next_run.outcome_summary,
                 next_run.error_summary, next_run.result_report,
                 run_id, current.status.value),
            )
            if cursor.rowcount != 1:
                raise InvalidJobRunTransitionError("job run changed concurrently")
            if focus_update is not None:
                if status is not JobRunStatus.COMPLETED or current.job_id != focus_job_id:
                    raise ValueError("focus requires exact completed Job ownership")
                self._validate_focus_policy(current.job_id, focus_definition)
                self._apply_focus(current.job_id, next_run, focus_update, "model",
                                  focus_task_id, focus_episode_id)
            if procedure_candidate is not None or procedure_feedback is not None:
                if current.job_id != procedure_job_id:
                    raise ValueError("procedure requires exact Job ownership")
                self.validate_procedure_policy(current.job_id, procedure_definition)
                evidence = self.get_run_procedure(run_id)
                if (evidence is None or evidence['job_id'] != current.job_id
                        or evidence['definition_identity'] != procedure_definition):
                    raise ValueError("procedure occurrence was not opted in")
                if procedure_candidate is not None:
                    if status is not JobRunStatus.COMPLETED:
                        raise ValueError("procedure nomination requires completed outcome")
                    disposition = self._nominate_procedure(current.job_id, procedure_candidate,
                        next_run, 'model', procedure_task_id, procedure_episode_id)
                    self._connection.execute("UPDATE job_run_procedures SET candidate_disposition=? WHERE run_id=?",
                                             (disposition, run_id))
                if procedure_feedback is not None:
                    if not isinstance(procedure_feedback, ProcedureFeedback):
                        raise TypeError("typed procedure feedback required")
                    snapshot = evidence['snapshot']
                    if (status not in (JobRunStatus.COMPLETED, JobRunStatus.FAILED)
                            or not evidence['supplied'] or snapshot is None
                            or snapshot['version'] != procedure_feedback.version):
                        raise ValueError("feedback requires the exact supplied procedure version")
                    self._connection.execute("UPDATE job_run_procedures SET feedback_json=? WHERE run_id=?",
                        (json.dumps(asdict(procedure_feedback)), run_id))
            self._connection.commit()
            return next_run
        except BaseException:
            self._connection.rollback()
            raise

    def focus_definition_identity(self, job_id: int) -> str:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError("unknown focus Job")
        schedule = self.get_schedule(job_id)
        definition = {
            "name": job.name, "description": job.description,
            "target": None if job.target is None else str(job.target),
            "triggers": [(row["event_type"], bool(row["enabled"]))
                         for row in self._connection.execute(
                             "SELECT event_type,enabled FROM job_triggers WHERE job_id=? ORDER BY event_type",
                             (job_id,))],
            # Keep the legacy daily encoding byte-for-byte compatible. Durable
            # admission markers do not change the informational focus definition.
            "schedule": None if schedule is None else
                        ((schedule.enabled, schedule.local_time, schedule.timezone)
                         if schedule.cadence == "daily" else
                         (schedule.enabled, "hourly", schedule.minute, schedule.timezone)),
        }
        return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()

    def set_carry_forward_enabled(self, job_id: int, enabled: bool) -> Job:
        if type(enabled) is not bool:
            raise TypeError("carry-forward enabled must be boolean")
        if self.get_job(job_id) is None:
            raise KeyError("unknown focus Job")
        self._connection.execute(
            "UPDATE jobs SET carry_forward_enabled=? WHERE id=?", (int(enabled), job_id))
        return self.get_job(job_id)

    def get_focus(self, job_id: int) -> JobFocus | None:
        _id(job_id, "job")
        row = self._connection.execute("SELECT * FROM job_focus WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        values = dict(row)
        for key in ("created_at", "updated_at", "source_finished_at"):
            values[key] = _parse(values[key])
        return JobFocus(**values)

    def _validate_focus_policy(self, job_id: int, definition: str | None) -> None:
        job = self.get_job(job_id)
        if job is None or not job.carry_forward_enabled:
            raise ValueError("carry-forward is disabled")
        if definition != self.focus_definition_identity(job_id):
            raise ValueError("focus Job definition changed")

    def administer_focus(self, job_id: int, proposal: JobFocusProposal,
                         source_run_id: int | None = None) -> JobFocus | None:
        """Exact completed Run seeds are operator-authored, never report quotations."""
        if not isinstance(proposal, JobFocusProposal):
            raise TypeError("focus requires a typed proposal")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            job = self.get_job(job_id)
            if job is None:
                raise KeyError("unknown focus Job")
            # Clear remains possible while disabled; disabling retains history.
            if proposal.action == "set":
                self._validate_focus_policy(job_id, self.focus_definition_identity(job_id))
                source = self.get_run(source_run_id)
                if source is None or source.job_id != job_id or source.status is not JobRunStatus.COMPLETED:
                    raise ValueError("focus seed requires an exact completed source Run belonging to this Job")
            else:
                source = None
            if proposal.basis is not None:
                raise ValueError("operator focus has no current episode evidence basis")
            self._apply_focus(job_id, source, proposal, "operator", None, None)
            focus = self.get_focus(job_id)
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        return focus

    def _apply_focus(self, job_id: int, source: JobRun | None,
                     proposal: JobFocusProposal, author: str,
                     task_id: str | None, episode_id: int | None) -> None:
        if not isinstance(proposal, JobFocusProposal):
            raise TypeError("focus requires a typed proposal")
        if proposal.action == "keep":
            return
        self._connection.execute("UPDATE jobs SET focus_version=focus_version+1 WHERE id=?", (job_id,))
        if proposal.action == "clear":
            self._connection.execute("DELETE FROM job_focus WHERE job_id=?", (job_id,))
            return
        version = self._connection.execute("SELECT focus_version FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        if source is None or source.job_id != job_id or source.status is not JobRunStatus.COMPLETED:
            raise ValueError("focus requires a completed source belonging to its Job")
        now = self._now()
        record = JobFocus(job_id, proposal.question, proposal.status, proposal.blocker,
                          source.id, author, task_id, episode_id, proposal.basis,
                          source.finished_at, now, now, version,
                          self.focus_definition_identity(job_id))
        values = asdict(record)
        for key in ("source_finished_at", "created_at", "updated_at"):
            values[key] = _format(values[key])
        columns = ",".join(values)
        parameters = ",".join("?" for _ in values)
        self._connection.execute(
            f"INSERT OR REPLACE INTO job_focus ({columns}) VALUES ({parameters})", tuple(values.values()))

    def validate_procedure_policy(self, job_id: int, definition: str | None) -> None:
        job = self.get_job(job_id)
        if job is None or not job.procedure_enabled:
            raise ValueError('procedure policy is disabled')
        if definition != self.focus_definition_identity(job_id):
            raise ValueError('procedure Job definition changed')

    def set_procedure_enabled(self, job_id: int, enabled: bool) -> Job:
        if type(enabled) is not bool:
            raise TypeError('procedure enabled must be boolean')
        if self.get_job(job_id) is None:
            raise KeyError('unknown procedure Job')
        self._connection.execute('UPDATE jobs SET procedure_enabled=? WHERE id=?', (int(enabled), job_id))
        return self.get_job(job_id)

    def create_procedure_experiment(self) -> Job:
        # Definition and policy are one explicit administrative transaction.
        self._connection.execute('BEGIN IMMEDIATE')
        try:
            job = self.create_job('Procedure Learning Experiment', EXPERIMENT_DESCRIPTION, enabled=False)
            self._connection.execute('UPDATE jobs SET procedure_experiment=1 WHERE id=?', (job.id,))
            job = self.set_procedure_enabled(job.id, True)
            self._connection.commit()
            return job
        except BaseException:
            self._connection.rollback()
            raise

    def get_procedure(self, job_id: int, state: str) -> dict | None:
        _id(job_id, 'job')
        if state not in ('active', 'pending'):
            raise ValueError('invalid procedure state')
        row = self._connection.execute(
            'SELECT record_json FROM job_procedures WHERE job_id=? AND state=?', (job_id, state)).fetchone()
        return None if row is None else json.loads(row[0])

    def _nominate_procedure(self, job_id: int, proposal: ProcedureProposal,
                            source: JobRun | None, author: str,
                            task_id: str | None = None, episode_id: int | None = None) -> str:
        if not isinstance(proposal, ProcedureProposal):
            raise TypeError('typed procedure proposal required')
        if source is not None and (source.job_id != job_id or source.status is not JobRunStatus.COMPLETED):
            raise ValueError('procedure source must be an exact completed Run belonging to this Job')
        if author == 'model':
            if source is None or not isinstance(task_id, str) or not task_id or len(task_id) > 100:
                raise ValueError('model procedure requires exact source Task')
            _id(episode_id, 'episode')
        elif author != 'operator' or task_id is not None or episode_id is not None or proposal.basis is not None:
            raise ValueError('invalid operator procedure provenance')
        if self.get_procedure(job_id, 'pending') is not None:
            return 'pending_exists'
        self._connection.execute('UPDATE jobs SET procedure_version=procedure_version+1 WHERE id=?', (job_id,))
        version = self._connection.execute('SELECT procedure_version FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
        record = dict(job_id=job_id, version=version, author=author,
                      source_run_id=source.id if source else None,
                      source_finished_at=_format(source.finished_at) if source else None,
                      source_task_id=task_id, source_episode_id=episode_id,
                      created_at=_format(self._now()), activated_at=None,
                      definition_identity=self.focus_definition_identity(job_id),
                      basis=proposal.basis,
                      instructions={key: value for key, value in asdict(proposal).items() if key != 'basis'})
        render_procedure(record)  # Validate projection bound before publishing.
        self._connection.execute('INSERT INTO job_procedures VALUES (?,?,?,?)',
                                 (job_id, version, 'pending', json.dumps(record)))
        return 'proposed'

    def seed_procedure(self, job_id: int, proposal: ProcedureProposal,
                       source_run_id: int | None = None) -> str:
        self._connection.execute('BEGIN IMMEDIATE')
        try:
            self.validate_procedure_policy(job_id, self.focus_definition_identity(job_id))
            source = None if source_run_id is None else self.get_run(source_run_id)
            if source_run_id is not None and source is None:
                raise ValueError('unknown procedure source Run')
            disposition = self._nominate_procedure(job_id, proposal, source, 'operator')
            self._connection.commit()
            return disposition
        except BaseException:
            self._connection.rollback()
            raise

    def review_procedure(self, job_id: int, version: int, *, activate: bool) -> None:
        _id(job_id, 'job')
        _id(version, 'procedure version')
        if type(activate) is not bool:
            raise TypeError('activate must be boolean')
        self._connection.execute('BEGIN IMMEDIATE')
        try:
            pending = self.get_procedure(job_id, 'pending')
            if pending is None or pending['version'] != version:
                raise ValueError('exact pending procedure version does not match')
            if activate:
                self.validate_procedure_policy(job_id, pending['definition_identity'])
                pending['activated_at'] = _format(self._now())
                render_procedure(pending)
                self._connection.execute("DELETE FROM job_procedures WHERE job_id=? AND state='active'", (job_id,))
                self._connection.execute("UPDATE job_procedures SET state='active',record_json=? WHERE job_id=? AND version=? AND state='pending'",
                                         (json.dumps(pending), job_id, version))
            else:
                self._connection.execute("DELETE FROM job_procedures WHERE job_id=? AND version=? AND state='pending'", (job_id, version))
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def capture_run_procedure(self, job_id: int, run_id: int) -> dict:
        """Persist selection once, before cognition; never recover execution on restart."""
        self._connection.execute('BEGIN IMMEDIATE')
        try:
            run = self.get_run(run_id)
            if run is None or run.job_id != job_id or run.status is not JobRunStatus.PENDING:
                raise ValueError('procedure snapshot requires exact pending occurrence')
            definition = self.focus_definition_identity(job_id)
            self.validate_procedure_policy(job_id, definition)
            active = self.get_procedure(job_id, 'active')
            if active is not None and active['definition_identity'] != definition:
                active = None
            if active is not None:
                active['snapshot_at'] = _format(self._now())
                render_procedure(active)
            # Retain a fixed per-Job window including this Run. Never delete live evidence.
            self._connection.execute('''DELETE FROM job_run_procedures WHERE run_id IN (
                SELECT p.run_id FROM job_run_procedures p JOIN job_runs r ON r.id=p.run_id
                WHERE p.job_id=? AND r.status NOT IN ('pending','running')
                ORDER BY p.run_id DESC LIMIT -1 OFFSET ?)''', (job_id, MAX_PROCEDURE_RUNS - 1))
            count = self._connection.execute('SELECT count(*) FROM job_run_procedures WHERE job_id=?', (job_id,)).fetchone()[0]
            if count >= MAX_PROCEDURE_RUNS:
                raise ValueError('procedure Run retention limit reached by live occurrences')
            self._connection.execute('''INSERT INTO job_run_procedures
                (run_id,job_id,definition_identity,snapshot_json) VALUES (?,?,?,?)''',
                (run_id, job_id, definition, json.dumps(active) if active else None))
            evidence = self.get_run_procedure(run_id)
            self._connection.commit()
            return evidence
        except BaseException:
            self._connection.rollback()
            raise

    def get_run_procedure(self, run_id: int) -> dict | None:
        _id(run_id, 'run')
        row = self._connection.execute('SELECT * FROM job_run_procedures WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            return None
        return dict(run_id=run_id, job_id=row['job_id'], definition_identity=row['definition_identity'],
                    snapshot=json.loads(row['snapshot_json']) if row['snapshot_json'] else None,
                    supplied=bool(row['supplied']),
                    feedback=json.loads(row['feedback_json']) if row['feedback_json'] else None,
                    candidate_disposition=row['candidate_disposition'])

    def mark_procedure_supplied(self, run_id: int) -> None:
        # A projection is a runtime fact, never a claim of use or effectiveness.
        self._connection.execute('''UPDATE job_run_procedures SET supplied=1
            WHERE run_id=? AND snapshot_json IS NOT NULL AND supplied=0''', (run_id,))

    def create_finding(self, job_id: int, run_id: int, task_id: str, episode_id: int,
                       topic: str, kind: FindingKind, claim: str,
                       evidence_basis: tuple[FindingEvidence, ...]) -> Finding:
        """Stage a Finding iff its exact source Run is still running."""
        now = self._now()
        probe = Finding(1, job_id, run_id, task_id, episode_id, topic, kind, claim,
                        evidence_basis, now)
        evidence_json = json.dumps([{
            "ordinal": item.ordinal, "capability": item.capability,
            "status": item.status, "class": item.evidence_class.value,
        } for item in probe.evidence_basis], ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT job_id,status FROM job_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError(f"job run does not exist: {run_id}")
            if row["job_id"] != job_id:
                raise ValueError("job run does not belong to finding job")
            if row["status"] != JobRunStatus.RUNNING.value:
                raise ValueError("source job run is not running")
            cursor = self._connection.execute(
                """INSERT INTO job_findings
                   (job_id,run_id,task_id,episode_id,topic,kind,claim,evidence_json,published_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (job_id, run_id, probe.task_id, episode_id, probe.topic, kind.value,
                 probe.claim, evidence_json, _format(now)))
            self._connection.commit()
            return Finding(cursor.lastrowid, job_id, run_id, probe.task_id, episode_id,
                           probe.topic, kind, probe.claim, evidence_basis, now)
        except BaseException:
            self._connection.rollback()
            raise

    def get_finding(self, finding_id: int) -> Finding | None:
        _id(finding_id, "finding")
        row = self._connection.execute(
            "SELECT * FROM job_findings WHERE id=?", (finding_id,)).fetchone()
        return None if row is None else _finding(row)

    def list_findings(self, *, limit: int = 20) -> tuple[Finding, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be from 1 to 100")
        rows = self._connection.execute(
            "SELECT * FROM job_findings ORDER BY published_at DESC,id DESC LIMIT ?",
            (limit,)).fetchall()
        return tuple(_finding(row) for row in rows)

    def search_findings(self, query: str, *, limit: int = 5) -> tuple[Finding, ...]:
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        normalized = " ".join(query.split()).casefold()
        if (not normalized or len(normalized) > MAX_FINDING_QUERY_CHARS
                or any(unicodedata.category(c) == "Cc" for c in normalized)):
            raise ValueError("query must be non-empty bounded text")
        if type(limit) is not int or not 1 <= limit <= MAX_FINDING_SEARCH_LIMIT:
            raise ValueError(f"limit must be from 1 to {MAX_FINDING_SEARCH_LIMIT}")
        tokens = tuple(dict.fromkeys(normalized.split()))
        # A bounded candidate set keeps scoring understandable without adding FTS.
        rows = self._connection.execute(
            """SELECT f.*,j.name AS job_name FROM job_findings f
               JOIN job_runs r ON r.id=f.run_id JOIN jobs j ON j.id=f.job_id
               WHERE r.status='completed' ORDER BY f.published_at DESC,f.id DESC""")
        # Stream the whole corpus but retain only the best bounded result window.
        # This deliberately trades O(corpus) lexical scan time for O(limit) Python
        # memory until a later phase introduces an indexed search subsystem.
        ranked: list[tuple[int, str, int, sqlite3.Row]] = []
        for row in rows:
            topic, claim, job_name = (row["topic"].casefold(), row["claim"].casefold(),
                                      row["job_name"].casefold())
            fields = (topic, claim, job_name)
            score = sum(3 if token == topic else 1 for token in tokens
                        if any(token in field for field in fields))
            if score:
                candidate = (score, row["published_at"], row["id"], row)
                ranked.append(candidate)
                ranked.sort(key=lambda item: item[:3], reverse=True)
                if len(ranked) > limit:
                    ranked.pop()
        return tuple(_finding(row) for _, _, _, row in ranked)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SQLiteJobStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _id(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} ID must be a positive integer")
    return value


def _format(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _job(row: sqlite3.Row) -> Job:
    target = None if row["target_kind"] is None else JobTarget(row["target_kind"], row["target_identifier"])
    return Job(row["id"], row["name"], row["description"], bool(row["enabled"]),
               target, _parse(row["created_at"]), _parse(row["updated_at"]),
               bool(row["carry_forward_enabled"]), bool(row["procedure_enabled"]),
               bool(row["procedure_experiment"]))  # type: ignore[arg-type]


def _run(row: sqlite3.Row) -> JobRun:
    return JobRun(row["id"], row["job_id"], JobRunStatus(row["status"]),
                  _parse(row["created_at"]), _parse(row["started_at"]),
                  _parse(row["finished_at"]), row["outcome_summary"],
                  row["error_summary"], row["result_report"])  # type: ignore[arg-type]


def _finding(row: sqlite3.Row) -> Finding:
    try:
        raw = json.loads(row["evidence_json"])
        if not isinstance(raw, list) or not raw:
            raise ValueError("invalid finding evidence")
        evidence = tuple(FindingEvidence(
            item["ordinal"], item["capability"], item["status"],
            FindingEvidenceClass(item["class"])) for item in raw)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"malformed evidence for FIND{row['id']}") from error
    return Finding(row["id"], row["job_id"], row["run_id"], row["task_id"],
                   row["episode_id"], row["topic"], FindingKind(row["kind"]),
                   row["claim"], evidence, _parse(row["published_at"]))  # type: ignore[arg-type]


def _schedule(row: sqlite3.Row) -> JobSchedule:
    return JobSchedule(row["job_id"], bool(row["enabled"]), row["local_time"],
                       row["timezone"], row["last_started_local_date"],
                       row["cadence"], row["minute"],
                       _parse(row["last_started_slot"]) if row["last_started_slot"] else None)
