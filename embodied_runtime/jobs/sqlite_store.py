"""SQLite-backed durable Job catalog."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

from .model import (
    InvalidJobRunTransitionError, Job, JobRun, JobRunStatus, JobSchedule, JobTarget,
    RUN_TRANSITIONS, TERMINAL_RUN_STATUSES,
)

SCHEMA_VERSION = 3
_SCHEMA = (
    """CREATE TABLE jobs (
       id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
       enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
       target_kind TEXT, target_identifier TEXT,
       created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
       CHECK((target_kind IS NULL AND target_identifier IS NULL) OR
             (target_kind IS NOT NULL AND target_identifier IS NOT NULL)))""",
    """CREATE TABLE job_runs (
       id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
       status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','stopped')),
       created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
       outcome_summary TEXT, error_summary TEXT, result_report TEXT)""",
    "CREATE INDEX idx_job_runs_job ON job_runs(job_id, id)",
    """CREATE TABLE job_schedules (
       job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE RESTRICT,
       enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
       local_time TEXT NOT NULL, timezone TEXT NOT NULL,
       last_started_local_date TEXT)""",
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
        if version in (1, 2):
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if version == 1:
                    self._connection.execute(_SCHEMA[-1])
                self._connection.execute("ALTER TABLE job_runs ADD COLUMN result_report TEXT")
                self._connection.execute("PRAGMA user_version = 3")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
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

    def create_run(self, job_id: int) -> JobRun:
        _id(job_id, "job")
        now = self._now()
        try:
            cursor = self._connection.execute(
                "INSERT INTO job_runs (job_id, status, created_at) VALUES (?, 'pending', ?)",
                (job_id, _format(now)),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"job does not exist: {job_id}") from error
        return JobRun(cursor.lastrowid, job_id, JobRunStatus.PENDING, now)

    def set_schedule(self, job_id: int, local_time: str, timezone: str, *,
                     enabled: bool = True) -> JobSchedule:
        schedule = JobSchedule(job_id, enabled, local_time, timezone)
        if self.get_job(job_id) is None:
            raise KeyError(f"unknown job: {job_id}")
        self._connection.execute(
            """INSERT INTO job_schedules(job_id, enabled, local_time, timezone)
               VALUES (?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET
               enabled=excluded.enabled, local_time=excluded.local_time,
               timezone=excluded.timezone""",
            (job_id, int(enabled), local_time, timezone),
        )
        return self.get_schedule(job_id)  # type: ignore[return-value]

    def get_schedule(self, job_id: int) -> JobSchedule | None:
        _id(job_id, "job")
        row = self._connection.execute(
            "SELECT * FROM job_schedules WHERE job_id=?", (job_id,)
        ).fetchone()
        return _schedule(row) if row is not None else None

    def list_schedules(self) -> tuple[JobSchedule, ...]:
        rows = self._connection.execute("SELECT * FROM job_schedules ORDER BY job_id").fetchall()
        return tuple(_schedule(row) for row in rows)

    def remove_schedule(self, job_id: int) -> bool:
        _id(job_id, "job")
        return self._connection.execute(
            "DELETE FROM job_schedules WHERE job_id=?", (job_id,)
        ).rowcount == 1

    def create_scheduled_run(self, job_id: int, local_date: str) -> JobRun | None:
        """Atomically consume today's schedule marker and create its occurrence."""
        probe = JobSchedule(job_id, True, "00:00", "UTC", local_date)
        now = self._now()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(
                """UPDATE job_schedules SET last_started_local_date=?
                   WHERE job_id=? AND enabled=1 AND
                   (last_started_local_date IS NULL OR last_started_local_date<>?)""",
                (probe.last_started_local_date, job_id, probe.last_started_local_date),
            )
            if cursor.rowcount != 1:
                self._connection.rollback()
                return None
            cursor = self._connection.execute(
                "INSERT INTO job_runs(job_id,status,created_at) VALUES(?,'pending',?)",
                (job_id, _format(now)),
            )
            self._connection.commit()
            return JobRun(cursor.lastrowid, job_id, JobRunStatus.PENDING, now)
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

    def transition_run(self, run_id: int, status: JobRunStatus, *,
                       outcome_summary: str | None = None,
                       error_summary: str | None = None,
                       result_report: str | None = None) -> JobRun:
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
            self._connection.commit()
            return next_run
        except BaseException:
            self._connection.rollback()
            raise

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
               target, _parse(row["created_at"]), _parse(row["updated_at"]))  # type: ignore[arg-type]


def _run(row: sqlite3.Row) -> JobRun:
    return JobRun(row["id"], row["job_id"], JobRunStatus(row["status"]),
                  _parse(row["created_at"]), _parse(row["started_at"]),
                  _parse(row["finished_at"]), row["outcome_summary"],
                  row["error_summary"], row["result_report"])  # type: ignore[arg-type]


def _schedule(row: sqlite3.Row) -> JobSchedule:
    return JobSchedule(row["job_id"], bool(row["enabled"]), row["local_time"],
                       row["timezone"], row["last_started_local_date"])
