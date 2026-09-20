# Jobs

Jobs are durable runtime infrastructure. Phase 2 lets an operator explicitly
coordinate one occurrence through the runtime's existing Task lifecycle. It
still does not autonomously execute, schedule, route, or retry work.

## Job

A **Job** is a durable responsibility with an integer catalog identity (shown
as `JOB1`), name, description, enabled flag, optional target, and timestamps.
Enabled Jobs may be explicitly started; disabled definitions remain visible but
cannot start a new run through the normal path. Neither state causes autonomous
execution, and definitions are retained.

A `JobTarget` is open-ended assignment metadata such as `body:camera`,
`agent:mira`, or `runtime:workshop-controller`. It is not proof that a reader is
eligible or authorized to execute the Job, and need not refer to attached
hardware.

> `target=None` means unassigned, not execute everywhere.

## JobRun

A **JobRun** is one durable occurrence, with its own `RUN1` identity, Job
foreign key, status, timestamps, and optional bounded outcome/error summaries:

```text
pending -> running -> completed
                   -> failed
                   -> stopped
pending ----------------> stopped
```

Completed, failed, and stopped runs are terminal. Transitions are atomic and
fail closed. Closing a store or application does not rewrite pending or running
runs.

## Runtime coordination through Task

A **Task** remains the runtime-owned bounded unit of meaningful work. Manual
start creates a pending JobRun, constructs exactly one ordinary Task and
TaskGoal, starts it through `RobotApplication.start_task()`, transitions the run
to running, and installs a read-only session-local association:

```text
Job                    durable responsibility
 |
JobRun                 durable occurrence
 |
runtime binding        volatile association
 |
Task                   bounded current work
 |
ActiveGoal/resources   existing Task-owned runtime state
```

The binding carries the full Job, current JobRun snapshot, and current Task
snapshot. It is deliberately not persisted: the Task UUID is not stored in the
Jobs database because Tasks cannot currently be recovered after restart. Task
coordination remains authoritative for goals, resources, pause/resume, and
terminal state; Jobs do not implement a parallel execution lifecycle.

Manual start creates bounded text such as `Run JOB1: Review logs` and `Complete
JOB1: Review logs`. It does not copy or truncate the potentially larger Job
description into the TaskGoal. Starting the Task creates its ActiveGoal through
the existing Task path and does **not** wake cognition, synthesize an operator
event, or invoke an LLM.

While a Task is paused, its JobRun remains `running`. Existing Task behavior
releases Task-owned resources and suspends its ActiveGoal; resume creates a
fresh Task-owned ActiveGoal without reacquiring resources. Completion, failure,
and stop map to matching Task and JobRun terminal statuses. Completed and
stopped summaries are outcomes; failed summaries are errors.

Task terminalization precedes durable JobRun terminalization. If persistence
then fails, the terminal Task is not restarted: the volatile binding is retained
fail-closed so the same terminal intent can be retried, while a conflicting
terminal retry is rejected.

## Catalogs, assignment, and claims

One Jobs SQLite database is one globally readable catalog. `list_jobs()` lists
the whole catalog unless its caller explicitly supplies a target filter. It
does not inspect `RobotProfile`, attached hardware, or the current process.

> Explicit manual start in Phase 2 is not automatic target eligibility
> resolution.

An operator may deliberately start any enabled Job regardless of its target.
The runtime neither compares the target to `RobotProfile` nor infers a physical
body identity. Target metadata, including unassigned, is preserved unchanged.

Catalog visibility, execution assignment, and a runtime execution claim remain
separate concepts. Multi-process automatic execution will require an atomic
claim or lease with a runtime identity. There is still no `claimed_by` column.

## Persistence, shutdown, and restart

Jobs use their own `SQLiteJobStore`, schema version 1, normally in
`data/jobs.sqlite3`. They are independent of persistent memory and Tasks. Jobs
and memory can each be enabled or disabled independently.

```toml
[jobs]
enabled = true
database_path = "data/jobs.sqlite3"
```

Shutdown releases the current Task's volatile ActiveGoal and resources, then
clears both Task and JobRun bindings before closing the store. It does not
invent semantic completion: a durable running occurrence remains running. On
restart it remains visible through `job runs`, while `current_job_run` and
`current_task` are empty. Automatic recovery is not implemented.

The console inspection commands are `jobs`, `job show JOB<n>`, and `job runs
JOB<n>`. Definition commands are `job add`, `job enable`, and `job disable`.
Runtime coordination commands are `job start`, `job current`, `job complete`,
`job fail`, and `job stop`.

Autonomous cognition/execution, scheduling, timers, Job-owned resources,
retries, body/runtime registries, target matching, distributed coordination,
claims, and restart recovery remain future work.
