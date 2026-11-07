"""Read-only accounting over retained JobRun metadata, never authored content."""

from datetime import UTC, datetime, timedelta
import sqlite3

from .model import JobRunStatus, TERMINAL_RUN_STATUSES

MAX_ACTIVITY_JOBS = 10
MAX_ACTIVITY_RUNS = 20
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_STATES = tuple(state.value for state in JobRunStatus)
_TERMINAL = tuple(state.value for state in JobRunStatus if state in TERMINAL_RUN_STATUSES)


def timestamp_us(value: object) -> int | None:
    """Normalize aware ISO timestamps exactly; unsafe/malformed values stay unknown."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None or instant.utcoffset() is None:
            return None
        delta = instant.astimezone(UTC) - _EPOCH
        return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    except (ValueError, OverflowError):
        return None


# Each flag is per record. SQL sums these before either display cap is applied.
_FLAGS = {
    "records": "1",
    "created": "created_us >= :start AND created_us < :end",
    "started": "COALESCE(started_us >= :start AND started_us < :end, 0)",
    **{f"terminal_{state}":
       f"COALESCE(finished_us >= :start AND finished_us < :end AND status = '{state}', 0)"
       for state in _TERMINAL},
    **{f"observed_{state}": f"status = '{state}'" for state in _STATES},
    "open_before_start": "created_us < :start AND (finished_us IS NULL OR finished_us >= :start)",
    "open_before_end": "created_us < :end AND (finished_us IS NULL OR finished_us >= :end)",
}
_SUMS = ", ".join(f"COALESCE(SUM({flag}), 0) AS {name}" for name, flag in _FLAGS.items())
_NAMES = ", ".join(_FLAGS)
_VALUES = ", ".join(f"({flag})" for flag in _FLAGS.values())

# One statement gives totals, groups and bounded details the same read snapshot.
# Raw nulls are retained separately: malformed non-null timestamps must not be
# mistaken for a legitimate absent start/finish. No Job-authored column is read.
_QUERY = f"""
WITH normalized AS MATERIALIZED (
    SELECT id, job_id, status,
           job_activity_time(created_at) AS created_us,
           job_activity_time(started_at) AS started_us,
           job_activity_time(finished_at) AS finished_us,
           started_at IS NULL AS no_start, finished_at IS NULL AS no_finish
    FROM job_runs
), checked AS MATERIALIZED (
    SELECT *, COALESCE(
        created_us IS NOT NULL AND created_us <= :retrieved
        AND (no_start OR (started_us IS NOT NULL AND started_us >= created_us AND started_us <= :retrieved))
        AND (no_finish OR (finished_us IS NOT NULL AND finished_us >= created_us AND finished_us <= :retrieved))
        AND (no_start OR no_finish OR started_us <= finished_us)
        AND ((status = 'pending' AND no_start AND no_finish)
             OR (status = 'running' AND NOT no_start AND no_finish)
             OR (status IN ('completed', 'failed') AND NOT no_start AND NOT no_finish)
             OR (status IN ('stopped', 'interrupted') AND NOT no_finish)), 0) AS valid
    FROM normalized
), qualifying AS MATERIALIZED (
    SELECT * FROM checked
    WHERE valid AND created_us < :end AND (finished_us IS NULL OR finished_us >= :start)
), job_groups AS (
    SELECT job_id, {_SUMS} FROM qualifying GROUP BY job_id
    ORDER BY job_id LIMIT {MAX_ACTIVITY_JOBS}
), selected_runs AS (
    SELECT *, CASE
        WHEN finished_us >= :start AND finished_us < :end AND status != 'completed' THEN 0
        WHEN finished_us IS NULL OR finished_us >= :end THEN 1
        ELSE 2 END AS priority
    FROM qualifying ORDER BY priority, id LIMIT {MAX_ACTIVITY_RUNS}
), metadata AS (
    SELECT COUNT(*) AS retained_records, COALESCE(SUM(NOT valid), 0) AS unclassified_records,
           (SELECT COUNT(DISTINCT job_id) FROM qualifying) AS qualifying_jobs FROM checked
)
SELECT 0 AS section, 0 AS priority, 0 AS ordinal,
       NULL AS job_id, NULL AS run_id, NULL AS status,
       NULL AS created_us, NULL AS started_us, NULL AS finished_us,
       {_SUMS},
       (SELECT retained_records FROM metadata) AS retained_records,
       (SELECT unclassified_records FROM metadata) AS unclassified_records,
       (SELECT qualifying_jobs FROM metadata) AS qualifying_jobs
FROM qualifying
UNION ALL
SELECT 1, 0, job_id, job_id, NULL, NULL, NULL, NULL, NULL,
       {_NAMES}, NULL, NULL, NULL FROM job_groups
UNION ALL
SELECT 2, priority, id, job_id, id, status, created_us, started_us, finished_us,
       {_VALUES}, NULL, NULL, NULL FROM selected_runs
ORDER BY section, priority, ordinal
"""


def _counts(row: sqlite3.Row) -> dict[str, object]:
    return {"records": row["records"], "created": row["created"], "started": row["started"],
            "terminal_outcomes": {state: row[f"terminal_{state}"] for state in _TERMINAL},
            "statuses_at_retrieval": {state: row[f"observed_{state}"] for state in _STATES},
            "open_before_start": row["open_before_start"], "open_before_end": row["open_before_end"]}


def summarize_activity(connection: sqlite3.Connection, start: datetime, end: datetime,
                       retrieved_at: datetime) -> dict[str, object]:
    """Aggregate valid record lifetimes; explicitly account for unclassifiable rows."""
    for instant in (start, end, retrieved_at):
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("activity window must be timezone-aware")
    if not start < end <= retrieved_at:
        raise ValueError("activity window must precede retrieval")
    parameters = {"start": timestamp_us(start.isoformat()), "end": timestamp_us(end.isoformat()),
                  "retrieved": timestamp_us(retrieved_at.isoformat())}
    rows = connection.execute(_QUERY, parameters).fetchall()
    totals = rows[0]
    jobs = [{"job_id": row["job_id"], "counts": _counts(row)} for row in rows if row["section"] == 1]
    runs = []
    for row in rows:
        if row["section"] != 2:
            continue
        runs.append({
            "job_id": row["job_id"], "run_id": row["run_id"], "status_at_retrieval": row["status"],
            **{name + "_at": (None if row[name + "_us"] is None else
                               (_EPOCH + timedelta(microseconds=row[name + "_us"])).isoformat())
               for name in ("created", "started", "finished")},
            "in_day": {"created": bool(row["created"]), "started": bool(row["started"]),
                       "terminal": any(row[f"terminal_{state}"] for state in _TERMINAL)},
            "open_before_start": bool(row["open_before_start"]),
            "open_before_end": bool(row["open_before_end"]),
        })
    return {
        "status": "ok", "source": "configured_jobs_database",
        "scope": "timestamp_consistent_retained_records", "record_authority": "runtime",
        "coverage": {"status": "partial" if totals["unclassified_records"] else "complete",
                     "retained_records": totals["retained_records"],
                     "unclassified_records": totals["unclassified_records"],
                     "unclassified_day_membership": "unknown"},
        "totals": _counts(totals), "jobs": jobs, "runs": runs,
        "detail_limits": {"jobs": MAX_ACTIVITY_JOBS, "runs": MAX_ACTIVITY_RUNS},
        "omitted_jobs": totals["qualifying_jobs"] - len(jobs),
        "omitted_runs": totals["records"] - len(runs),
    }
