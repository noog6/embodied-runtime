"""Volatile, evidence-backed progress for one exact Job occurrence."""

from dataclasses import dataclass
import re
from uuid import UUID


MAX_JOB_PROGRESS_COUNTERS = 8
MAX_JOB_PROGRESS_COUNTER_NAME_CHARS = 48
MAX_JOB_PROGRESS_COUNTER_VALUE = 1000
JOB_PROGRESS_BASES = frozenset((
    "wake_event", "acquisition_1", "acquisition_2", "effect_1", "effect_2",
))
_COUNTER_NAME = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")


@dataclass(frozen=True, slots=True, order=True)
class JobProgressCounter:
    name: str
    value: int


@dataclass(frozen=True, slots=True)
class JobProgressUpdate:
    """A model-proposed counter name bound to runtime evidence, never a value."""

    counter: str
    basis: str


@dataclass(frozen=True, slots=True)
class JobProgress:
    """Immutable counter snapshot scoped to an exact JobRun and Task."""

    job_id: int
    run_id: int
    task_id: UUID
    counters: tuple[JobProgressCounter, ...] = ()

    def increment(self, update: JobProgressUpdate) -> "JobProgress":
        validate_counter_name(update.counter)
        values = {counter.name: counter.value for counter in self.counters}
        if update.counter not in values and len(values) >= MAX_JOB_PROGRESS_COUNTERS:
            raise ValueError(
                f"Job progress permits at most {MAX_JOB_PROGRESS_COUNTERS} counters"
            )
        old = values.get(update.counter, 0)
        if old >= MAX_JOB_PROGRESS_COUNTER_VALUE:
            raise ValueError(
                f"Job progress counter is already at maximum "
                f"{MAX_JOB_PROGRESS_COUNTER_VALUE}"
            )
        values[update.counter] = old + 1
        return JobProgress(
            self.job_id, self.run_id, self.task_id,
            tuple(JobProgressCounter(name, value)
                  for name, value in sorted(values.items())),
        )

    def render(self) -> str:
        lines = [
            "Current Job progress",
            "These counters are volatile runtime-owned progress for this exact JobRun "
            "and Task. Each increment was committed from an accepted current-episode "
            "runtime evidence basis. They may support reasoning within this occurrence; "
            "they are not global memory or general claims about the outside world.",
        ]
        lines.extend(
            (f"  {counter.name}: {counter.value}" for counter in self.counters)
            if self.counters else ("  none",)
        )
        return "\n".join(lines)


def validate_counter_name(name: object) -> str:
    if not isinstance(name, str) or not _COUNTER_NAME.fullmatch(name):
        raise ValueError(
            "progress counter must match [a-z][a-z0-9_]{0,47}"
        )
    return name
