"""Immutable domain records for durable responsibilities and their runs."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_JOB_NAME_CHARS = 200
MAX_JOB_DESCRIPTION_CHARS = 2000
MAX_RUN_SUMMARY_CHARS = 2000
MAX_RUN_REPORT_CHARS = 8_000
_TOKEN = re.compile(r"^[^\W\d][\w-]*$", re.UNICODE)
_LOCAL_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")


@dataclass(frozen=True, slots=True)
class JobSchedule:
    """One durable daily, explicitly zoned activation schedule."""

    job_id: int
    enabled: bool
    local_time: str
    timezone: str
    last_started_local_date: str | None = None

    def __post_init__(self) -> None:
        _positive_id(self.job_id, "job ID")
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be boolean")
        if not isinstance(self.local_time, str) or _LOCAL_TIME.fullmatch(self.local_time) is None:
            raise ValueError("local_time must use strict 24-hour HH:MM format")
        if not isinstance(self.timezone, str):
            raise TypeError("timezone must be a string")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown IANA timezone: {self.timezone!r}") from error
        if self.last_started_local_date is not None:
            try:
                datetime.strptime(self.last_started_local_date, "%Y-%m-%d")
            except (TypeError, ValueError) as error:
                raise ValueError("last_started_local_date must use YYYY-MM-DD") from error


def _positive_id(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _text(value: str, label: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    value = value.strip()
    if not value and not empty:
        raise ValueError(f"{label} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{label} must be at most {limit} characters")
    return value


def _time(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class JobTarget:
    """Open-ended assignment metadata; it does not grant execution authority."""

    kind: str
    identifier: str

    def __post_init__(self) -> None:
        for field in ("kind", "identifier"):
            value = _text(getattr(self, field), field, 100)
            if _TOKEN.fullmatch(value) is None:
                raise ValueError(f"{field} must be a simple identifier")
            object.__setattr__(self, field, value)

    def __str__(self) -> str:
        return f"{self.kind}:{self.identifier}"


@dataclass(frozen=True, slots=True)
class Job:
    """An immutable snapshot of one durable responsibility."""

    id: int
    name: str
    description: str
    enabled: bool
    target: JobTarget | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _positive_id(self.id, "job ID")
        object.__setattr__(self, "name", _text(self.name, "name", MAX_JOB_NAME_CHARS))
        object.__setattr__(self, "description", _text(
            self.description, "description", MAX_JOB_DESCRIPTION_CHARS, empty=True
        ))
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be boolean")
        if self.target is not None and not isinstance(self.target, JobTarget):
            raise TypeError("target must be a JobTarget or None")
        object.__setattr__(self, "created_at", _time(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _time(self.updated_at, "updated_at"))


class JobRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


TERMINAL_RUN_STATUSES = frozenset((
    JobRunStatus.COMPLETED, JobRunStatus.FAILED, JobRunStatus.STOPPED,
))
RUN_TRANSITIONS = {
    JobRunStatus.PENDING: frozenset((JobRunStatus.RUNNING, JobRunStatus.STOPPED)),
    JobRunStatus.RUNNING: TERMINAL_RUN_STATUSES,
    JobRunStatus.COMPLETED: frozenset(),
    JobRunStatus.FAILED: frozenset(),
    JobRunStatus.STOPPED: frozenset(),
}


class InvalidJobRunTransitionError(ValueError):
    """Raised when a durable run transition is not in the lifecycle contract."""


@dataclass(frozen=True, slots=True)
class JobRun:
    """An immutable snapshot of one occurrence of a Job."""

    id: int
    job_id: int
    status: JobRunStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    outcome_summary: str | None = None
    error_summary: str | None = None
    result_report: str | None = None

    def __post_init__(self) -> None:
        _positive_id(self.id, "job run ID")
        _positive_id(self.job_id, "job ID")
        if not isinstance(self.status, JobRunStatus):
            raise TypeError("status must be a JobRunStatus")
        object.__setattr__(self, "created_at", _time(self.created_at, "created_at"))
        for field in ("started_at", "finished_at"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _time(value, field))
        for field in ("outcome_summary", "error_summary"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _text(value, field, MAX_RUN_SUMMARY_CHARS))
        if self.result_report is not None:
            object.__setattr__(self, "result_report", _text(
                self.result_report, "result_report", MAX_RUN_REPORT_CHARS
            ))
        if self.status in TERMINAL_RUN_STATUSES and self.finished_at is None:
            raise ValueError("terminal job runs require finished_at")
        if self.status not in TERMINAL_RUN_STATUSES and self.finished_at is not None:
            raise ValueError("non-terminal job runs cannot have finished_at")
        if self.status not in TERMINAL_RUN_STATUSES and self.result_report is not None:
            raise ValueError("non-terminal job runs cannot have a result report")
        if self.status is JobRunStatus.PENDING and self.started_at is not None:
            raise ValueError("pending job runs cannot have started_at")
