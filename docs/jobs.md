# Jobs

Jobs are durable runtime infrastructure. Phase 3 lets an operator explicitly
coordinate one occurrence through the runtime's existing Task lifecycle and
request one bounded work episode. It does not loop, schedule, route, or retry
work.

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

## Explicit bounded work

`job work` operates only on the current volatile JobRun association:

```text
Job
 |
JobRun
 |
Task / ActiveGoal
 |
explicit "job work"
 |
one bounded cognition/execution episode
 |
Job outcome evaluation
 |
completed | failed | continue
```

This is runtime-owned work, not operator dialogue. The exact Job ID, run ID,
name, full description, target (or `unassigned`), Task identity and description,
and bounded TaskGoal are supplied as explicit authoritative cognition context.
No synthetic operator utterance or WorkingMemory conversation turn is created.
The full Job description remains separate from the short TaskGoal.

The episode uses the shared attention single-flight coordinator and the existing
bounded initiative machinery, including its acquisition, effect, continuation,
resource, and Task-owned camera authority. Job work cannot use generic
`complete_goal`; its final decision instead passes through a separate
`report_job_outcome` tool and an exact Job/JobRun/Task/ActiveGoal binding check.
Only an accepted `completed` or `failed` report delegates to the authoritative
`finish_job_run()` lifecycle path. Missing, invalid, stale, or conflicting
reports conservatively mean `continue` and do not create a durable transition.
The outcome tool accepts only a proposal during cognition. A terminal proposal
is committed only after the entire outcome request returns successfully and the
exact binding is revalidated; a provider failure or stale binding before that
commit leaves the durable run unchanged.

The `schedule_followup` capability is omitted from the initial decision,
post-acquisition decisions, and effect continuation for Job work. Other
capabilities retain their normal configuration and availability checks.
`continue` never recursively schedules another work episode and the one-shot
executor always returns to the runtime. With automatic continuation disabled
(the default), another episode occurs only after another explicit `job work`
invocation. There is no Job-owned resource authority.

## Cooperative closed-loop continuation

Phase 4 optionally gives the exact, explicitly started current JobRun another
bounded work opportunity on a runtime heartbeat. This is closed-loop
continuation, not Job scheduling: scheduling would decide when to start a new
JobRun, while continuation only offers another turn to the already-bound
JobRun/Task. A manual `continue` arms a volatile grant of `max_auto_steps`.
Each accepted heartbeat consumes one step and schedules one separately owned,
finite invocation of the same Job executor. A further `continue` yields fully
before the next heartbeat; there is no cognition-owned or recursive loop.
The automatic step is accepted, and its budget charged, only after the shared
attention coordinator grants the Job episode. Losing that claim to an operator
or another episode is a deferral and leaves the grant unchanged.

The volatile record binds Job ID, JobRun ID, and Task UUID, and records
`armed` or `awaiting_operator`, the remaining step count, and last summary. It
does not bind the ActiveGoal, so a paused Task retains the grant and a resumed
Task can proceed using its fresh, exact Task-owned ActiveGoal. Paused Tasks,
operator waiters, and another active attention episode defer without consuming
a step. Operator attention wins before automatic work starts; an already-started
finite episode is not preempted.

Budget exhaustion leaves the Task and JobRun running in `awaiting_operator`;
an explicit `job work` can grant a fresh burst. An automatic provider failure
also moves to `awaiting_operator` and is not automatically retried. Terminal
Job operations clear the record.

Continuation is session-local, volatile, heartbeat-driven, bounded,
operator-fair, and non-recovering. The heartbeat neither scans durable running
rows nor discovers or starts enabled Jobs. Restart does not resume work, and
there are no execution claims, multi-runtime adoption, or persistent
continuation records.

## Daily local-time activation

An ordinary Job may have one durable daily schedule: an enabled flag, strict
24-hour `HH:MM` local time, explicit IANA timezone, and the last local calendar
date whose occurrence was created. `job schedule JOB1 daily 02:00` uses the
configured runtime timezone; `--timezone America/Toronto` makes it explicit,
`job schedule JOB1` inspects it, and `job unschedule JOB1` removes only the
schedule, never the Job or its history.

At each lightweight scheduler opportunity the application orders due schedules
by scheduled instant and Job ID. Schedule-local ineligibility (a disabled
schedule, missing Job, or disabled Job) is skipped so it cannot starve a later
eligible schedule. A runtime-wide blocker (current work, unavailable cognition,
operator waiting, or occupied attention) ends that opportunity. The first
eligible schedule starts, so at most one occurrence is activated. A time is due at its
exact minute or any later time on that same local date, so restart at 08:00
catches up a missed 02:00 occurrence but never backfills older dates. Disabled
Jobs and busy Task, goal, Job, operator, attention, or Job-work state defer
without consuming the date. The store atomically updates
`last_started_local_date` and creates the pending JobRun in one transaction;
repeated checks and ordinary restart therefore cannot create another occurrence
for that Job/date.

After Task binding, scheduled activation claims the shared attention
coordinator and starts exactly one invocation of the existing bounded Job work
executor. A `continue` result arms the existing Phase 4 heartbeat grant. There
is no scheduling-specific cognition loop. Shutdown stops the schedule timer
before cancelling work and dropping volatile Task coordination. A durable
running occurrence is not adopted after restart. Cross-runtime execution
claims and multi-body eligibility remain future work.

The timer offers an immediate check at startup before its first poll sleep.
Ordinary check exceptions are logged and followed by the normal sleep before a
later opportunity; they neither kill the timer nor retry Job work. Cancellation
still stops the timer promptly.

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
auto_continue = true
heartbeat_seconds = 30
max_auto_steps = 3
```

Shutdown first stops the continuation heartbeat, cancels and joins in-flight
Job work, and clears continuation. It then releases the current Task's volatile
ActiveGoal and resources, and
clears both Task and JobRun bindings before closing the store. It does not
invent semantic completion: a durable running occurrence remains running. On
restart it remains visible through `job runs`, while `current_job_run` and
`current_task` are empty. Automatic recovery is not implemented.

The console inspection commands are `jobs`, `job show JOB<n>`, and `job runs
JOB<n>`. Definition commands are `job add`, `job enable`, and `job disable`.
Runtime coordination commands are `job start`, asynchronous `job work`, `job
current`, `job complete`, `job fail`, and `job stop`.

It does not mark the durable run failed; the occurrence remains `running`, with
the same restart semantics. Cron, new-Job scheduling, Job-owned resources,
unbounded retries, body/runtime registries, target matching, distributed
coordination, claims, and restart recovery remain future work.
