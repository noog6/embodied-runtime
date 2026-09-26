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

## Durable Job results

A terminal JobRun may retain an optional `result_report` of at most 8,000
characters. The existing outcome or error summary remains the concise lifecycle
description; the report is a richer bounded work product or set of findings for
that one exact occurrence. Reports are written atomically with the accepted
terminal transition and cannot be appended, replaced, or attached to a
non-terminal run. Older runs naturally have no report.

These concepts remain deliberately separate:

| Concept | Role |
| --- | --- |
| Job | Durable responsibility |
| JobRun | Durable occurrence and terminal result owner |
| Task | Bounded execution authority |
| Job progress | Runtime-owned progress for the active occurrence |
| Semantic continuity | Volatile, non-authoritative context between episodes |
| JobRun result | Durable, bounded cognition work product after terminalization |
| Job Workspace | Mutable durable working material owned by the Job across runs |

A result may summarize or interpret evidence, but durability does not make the
text runtime evidence. It cannot change JobRun or Task status, modify progress,
or prove an external-world effect; the existing runtime validation and evidence
remain authoritative. A JobRun result is bounded text, not a Job Workspace,
artifact, attachment, or filesystem path.

**Storage is not delivery.** Persisting or retrieving a result does not speak,
notify, route, retry, mark read, or acknowledge it. Operators may inspect exact
occurrences with `job result RUN<n>` or the newest completed occurrence
(excluding newer running, failed, and stopped runs) with `job latest-result
JOB<n>`.

During explicit operator dialogue, `inspect_job_result(selector)` is a
read-only acquisition of one historical result. `RUN<n>` selects that exact
terminal occurrence (completed, failed, or stopped), while `JOB<n>` selects the
newest completed occurrence. A selector may instead be an exact Job name after
surrounding whitespace is trimmed. Name comparison is case-sensitive Unicode
code-point equality: there is no substring, fuzzy, semantic, vector, or temporal
matching. Duplicate exact names return at most ten candidate Job IDs and names
as an explicit ambiguity. Pending and running exact occurrences return an
explicit non-terminal state rather than current Task or continuation details.

The runtime owns the fact that a particular JobRun completed and owns its
durable identifiers, status, and timestamps. The stored summary/report is
historical cognition-authored work product and does not become fresh runtime
evidence merely because it is durable. Cognition must attribute mutable claims
to that earlier occurrence unless independent fresh evidence supports a current
claim. This differs both from bounded runtime diagnostics (BRD), which reports
current configured or recently observed runtime evidence, and persistent
memory, which stores durable semantic knowledge associated with entities.

The acquisition is offered only in explicit operator dialogue when Jobs
persistence exists. It shares the ordinary two-acquisition operator budget and
duplicate-call reuse. It is deliberately absent from autonomous initiative,
Job work, automatic continuation, and goal-directed attention so historical
cognition-authored prose cannot become Job outcome evidence. Retrieval creates
no JobRun, memory, readiness, progress, artifact, delivery, or acknowledgement.

Workspace inspection is a separate operator-only acquisition path. Exact
`JOB<n>` or exact case-sensitive Job-name selectors resolve the Job itself, so a
Workspace remains inspectable when the Job has never had a run. `workspace_list`
and `workspace_read` share the ordinary two-acquisition operator budget and are
not projected into autonomous or bounded Job work. Runtime-observed file
metadata is authoritative for the retrieved storage snapshot; artifact prose is
authored non-authoritative working material, not a JobRun result, persistent
memory, progress evidence, or fresh current-world evidence. The separate
`workspace_write(job, path, mode, content)` operator effect permits one
explicitly authorized bounded create, replace, or append per dialogue episode.
It is not projected into autonomous initiative, continuation, or Job work and
does not create a run or alter Tasks, progress, persistent memory, or immutable
JobRun results. There is no model-facing delete/move or `expected_version` in
this phase. See [Job Workspaces](job-workspaces.md).

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

### Continuation readiness

A continuing Job occurrence separates its non-terminal outcome from when
another bounded work episode is useful. `ready` permits work at the next
ordinary heartbeat opportunity. `after_delay` suppresses autonomous work until
a bounded delay of 1 through 86,400 seconds has elapsed on the monotonic clock;
the heartbeat may run it later, and expiry neither bypasses operator fairness
nor consumes an automatic step. `wait_for_operator` suppresses autonomous work
until the operator explicitly invokes `job work`. Unrelated operator interaction
does not resume it. Explicit work may override either waiting readiness and
receives the latest valid semantic continuity summary.

`wait_for_event` fully yields until a supported semantic runtime event occurs.
The initial catalog contains only `presence_changed`, mapped by the harness to
`PresenceChanged`; model output cannot name Python classes or provide predicates.
The event must arrive after the exact wait is armed. It makes one later episode
eligible but does not run cognition in the event handler, claim attention, or
consume budget. The projected `present` boolean is a bounded runtime-authored
fact; it establishes reported presence only, not a person's identity, object
visibility, safety, or unrelated mutable conditions. Fresh acquisition remains
necessary for facts outside that payload.

Event satisfaction remains sticky through pause, operator fairness, and busy
attention, and matching events coalesce rather than queue. Acceptance through
the ordinary continuation gate consumes the one wake context even if the
provider then fails. If that episode selects `wait_for_event` again, a new later
event is required. Explicit `job work` may instead supersede either a satisfied
or unsatisfied event wait; it receives semantic continuity but no invented wake
event.

Readiness is volatile, belongs to one exact JobRun/Task binding, and is removed
on terminal state, shutdown, or restart. A daily schedule starts a new
occurrence; readiness only gates another episode of that same occurrence.
Events never discover or create JobRuns, and no selector, payload, subscription,
or event history is persisted.

### Semantic continuity

A continuing Job occurrence may carry the latest work summary into its next
bounded episode. This projection comes directly from the volatile
`JobContinuation.last_summary`, is limited to 750 characters, and is scoped to
the exact Job ID, JobRun ID, and Task UUID. It exists only to preserve immediate
progress context. It is not authoritative evidence about the current world or
runtime state and cannot independently establish a mutable condition or a
terminal Job outcome; fresh acquisitions and current effect results remain the
authoritative evidence. Prior progress may still guide what should be inspected
next.

Only the latest episode summary is retained, rather than a transcript or
cumulative history. Manual work and heartbeat work use the same projection;
the first episode of a manual or scheduled occurrence has none. Pause/resume may
retain it because the occurrence and Task identity remain the same, while
terminalization, a new JobRun, shutdown, or a stale binding removes or rejects
it. The summary is not persisted, recovered, regenerated, or written as a
checkpoint or planner state.

### Evidence-backed occurrence progress

The runtime keeps four deliberately separate answers while bounded Job work is
active:

| Concept | Question answered | Authority |
| --- | --- | --- |
| Job | What am I responsible for? | Durable definition context |
| Semantic continuity | Where did the previous model episode leave off? | Model-generated, non-authoritative context |
| Job progress | What bounded evidence-backed occurrence progress was committed? | Runtime-owned for this exact JobRun and Task |
| Readiness / wake | When is another episode useful, and what current event made it eligible? | Runtime-owned scheduling and one-shot evidence |

`JobProgress` is an immutable, volatile snapshot bound to the exact Job ID,
JobRun ID, and Task UUID. It contains at most eight counters, ordered by name.
Names must match `[a-z][a-z0-9_]{0,47}`, and values are nonnegative integers no
greater than 1,000. An occurrence starts with an empty snapshot. Cognition can
never set a value, delta, decrement, or reset: a continuing
`report_job_outcome` may propose zero or one object of this exact form:

```json
{"counter": "presence_changes_seen", "basis": "wake_event"}
```

The operation is always an increment of exactly one performed by the harness.
The supported bases are `wake_event`, `acquisition_1`, `acquisition_2`,
`effect_1`, and `effect_2`. The harness deterministically accepts a basis only
when that evidence exists in the current exact episode: the wake was actually
supplied to the accepted Job episode, or the indexed acquisition/effect has an
`applied` runtime result. Rejected attempts, nonexistent indexes, earlier
episodes, semantic continuity, working memory, and model commentary cannot back
an increment. Terminal outcomes must carry no update.

The harness attests that each counter increment was backed by an accepted
current-episode runtime evidence source. Counter naming and task-level
interpretation remain cognition-owned; progress counters are occurrence-scoped
and are not general world-state assertions. This makes progress stronger than
semantic continuity without turning it into global factual memory. A current
wake remains separate evidence: committed progress can establish one earlier
step while the one-shot wake establishes the current step.

An accepted proposal is staged during the outcome tool callback. It commits
only after the provider response finishes successfully and the exact
Job/JobRun/Task/ActiveGoal binding is revalidated. Provider failure, stale
binding, rejection, terminalization, or shutdown cannot commit speculative
progress. Provider failure after an event wake retains the existing behavior:
the wake and automatic step remain consumed, progress remains unchanged, and
continuation awaits the operator.

Progress survives manual, heartbeat, and event-driven work, readiness changes,
pause/resume of the same Task, and automatic-budget exhaustion. These events do
not add steps or refill the budget. Scheduled activation starts with an empty
snapshot and later episodes of that same occurrence use the normal rules;
schedule markers are unrelated. Terminal Job completion, failure or stop, a
new occurrence, stale association, and application shutdown remove the
snapshot. It is not persisted or recovered. Non-Job attention and operator
conversation receive no progress-writing tool and cannot mutate it.
When an outcome callback becomes stale before commit, the runtime physically
clears only the snapshot owned by that stale occurrence; it cannot clear a
replacement occurrence's progress.

This facility is counters only. It is not a generic key/value store, arbitrary
checkpoint object, planner state, transcript, event history, or semantic-memory
write.

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

The console inspection commands are `jobs`, `job show JOB<n>`, `job runs
JOB<n>`, `job files JOB<n> [directory]`, and `job file JOB<n> <logical-path>
[offset_chars]`. Definition commands are `job add`, `job enable`, and `job disable`.
Runtime coordination commands are `job start`, asynchronous `job work`, `job
current`, `job complete`, `job fail`, and `job stop`.

It does not mark the durable run failed; the occurrence remains `running`, with
the same restart semantics. Cron, new-Job scheduling, Job-owned resources,
unbounded retries, body/runtime registries, target matching, distributed
coordination, claims, and restart recovery remain future work.

Each durable Job also owns one durable contained text Workspace shared across
its occurrences. It remains separate from immutable JobRun result reports and
from all volatile coordination state. See [Job Workspaces](job-workspaces.md).
