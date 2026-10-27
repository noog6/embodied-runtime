# Jobs

For an operator-installed ordinary Job connecting earlier accounts to selected
historical sources, see the [reflective continuity experiment](reflective-continuity.md).
It adds no runtime mechanism or startup Job.

This page specifies how the generic Job machinery works. For a concise gallery
of practical compositions exercised during Mira development, see [Tested Job
patterns](tested-jobs.md); those examples are not built-in Jobs.

See [Findings and deliberate search](findings.md) for reusable, provenance-bearing
claims published by completed JobRuns. Findings complement rather than replace
JobRun result reports and Job-owned Workspaces. See [bounded shared reads](job-shared-reads.md)
for opt-in historical consultation across Jobs without shared mutation authority.

See [Carry-forward Job focus](job-focus.md) for opt-in, bounded unresolved questions
projected into later occurrences of the same Job without acquisition. It is disabled
by default and grants no activation or action authority.

## Retained report retrieval and delivery

Storage, retrieval, and delivery are separate runtime operations. A JobRun's
`result_report` is an immutable occurrence work product; a Job Workspace artifact is
mutable, durable working material. In explicit operator dialogue, `retrieve_report`
can resolve an exact `RUN<n>`, the latest completed `JOB<n>`, or an exact
case-sensitive Job name using the same fail-closed rules as `inspect_job_result`.
Only the resolved run is considered: a missing report does not cause a backward
search. Retrieval returns an episode-local reference to an exact snapshot and does
not turn historical authored claims into authoritative current evidence.

P1 does not project report retrieval or delivery into Job execution, scheduling,
continuations, outcome cognition, or autonomous attention. Automatic completion
routing remains deferred; `address_operator` remains the concise autonomous
notification mechanism.

Jobs are durable runtime infrastructure. Explicit operator coordination starts
an occurrence through the runtime's Task lifecycle and requests bounded work.
Configured calendar/event activation and cooperative continuation provide further
work opportunities under the rules below; no model-controlled execution loop is
created.

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
                   -> interrupted
pending ----------------> stopped
        ----------------> interrupted
```

Completed, failed, stopped, and interrupted runs are terminal. `interrupted`
means execution continuity was lost because the owning runtime ended; it is not
successful completion, a semantic failure outcome, or a resumable checkpoint.
Both pending and running runs may transition to interrupted, and no transition
is allowed out of interrupted. Transitions are atomic and fail closed.

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
| Carry-forward focus | One opt-in historical question for future occurrences of the same Job |
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
Job work, automatic continuation, and goal-directed attention. With the separate
opt-in [shared-read policy](job-shared-reads.md), eligible Job work instead gets
`shared_inspect_job_run(selection, selector)` with explicit exact-Run versus
latest-completed semantics. Its historical content is excluded from current
Job-progress evidence; it does not change the operator tool's availability. Retrieval creates
no JobRun, memory, readiness, progress, artifact, delivery, or acknowledgement.

Workspace inspection has operator and Job-owned authority paths, plus the
independent opt-in source-selecting shared-read path. In operator dialogue, exact
`JOB<n>` or exact case-sensitive Job-name selectors resolve the Job itself, so a
Workspace remains inspectable when the Job has never had a run. `workspace_list`
and `workspace_read` share the ordinary two-acquisition operator budget and are
not projected into general autonomous work. Runtime-observed file
metadata is authoritative for the retrieved storage snapshot; artifact prose is
authored non-authoritative working material, not a JobRun result, persistent
memory, progress evidence, or fresh current-world evidence. The separate
`workspace_write(job, path, mode, content)` operator effect permits one
explicitly authorized bounded create, replace, append, or complete
create-or-replace `upsert` per dialogue episode. `create` requires absence;
`replace` and `append` require presence; `upsert` is for a complete artifact
whose prior existence is unknown. In particular, durable baselines and
current-state summaries rewritten across runs can use `upsert` so cognition
avoids spending an acquisition merely to discover whether the working artifact
has already been initialized.
It is not projected into general autonomous initiative or continuation and
does not create a run or alter Tasks, progress, persistent memory, or immutable
JobRun results. There is no model-facing delete/move or `expected_version` in
this phase. See [Job Workspaces](job-workspaces.md).

In bounded Job work, contextual list/read/write schemas omit the selector. The
runtime supplies only the owner from the exact current Job/JobRun/Task/ActiveGoal
binding. Reads use the existing two-acquisition budget and remain working
context, never progress evidence; writes use an existing effect opportunity and
prove only that a mutation occurred. JobRun results and Workspace artifacts can
both be produced in one occurrence, persist independently, and are never copied
or durably linked automatically.

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

Job cognition uses a bounded set of independent semantic-execution slots, sized
by `jobs.max_concurrent_work`, and uses the existing bounded initiative machinery,
including its acquisition, effect, continuation, resource, and Task-owned camera
authority. Each live occurrence owns an exact `JobExecutionContext` containing
its JobRun, Task, Job-bound ActiveGoal, continuation, progress, active work task,
Workspace authority, and Task-derived resource identity. Multiple different Jobs
may execute cognition concurrently; another activation of the same Job still
coalesces to its one active occurrence. Operator cognition remains independent of
Job capacity and does not preempt already-running Job work. General autonomous
attention remains conservatively separate. `ResourceArbiter` remains authoritative
for genuinely exclusive physical and effect resources. Job work cannot use generic
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

Phase 4 optionally gives an exact live JobRun context another bounded work
opportunity on a runtime heartbeat. Manually started, trigger-started, and
schedule-started occurrences may use this policy when configured. This is closed-loop
continuation, not Job scheduling: scheduling would decide when to start a new
JobRun, while continuation only offers another turn to the already-bound
JobRun/Task and never creates a new occurrence. A `continue` outcome arms that
context's volatile grant of `max_auto_steps`.
Each accepted heartbeat consumes one step and schedules one separately owned,
finite invocation of the same Job executor. A further `continue` yields fully
before the next heartbeat; there is no cognition-owned or recursive loop.
The automatic step is accepted, and its budget charged, only after the attention
coordinator admits the exact Job episode within the configured capacity. Operator activity or
an operator waiter prevents admission of new autonomous Job work and leaves the
grant unchanged. An autonomous Job episode that has already started is not
preempted when an operator arrives. Readiness and execution remain bound to the
exact context and the configured bounded Job work capacity.

The volatile record binds Job ID, JobRun ID, and Task UUID, and records
`armed` or `awaiting_operator`, the remaining step count, and last summary. It
does not bind the ActiveGoal, so a paused Task retains the grant and a resumed
Task can proceed using its fresh, exact Task-owned ActiveGoal. Paused Tasks,
operator activity or waiters, general autonomous initiative, and exhausted Job
capacity defer without consuming a step. Operator attention wins before automatic work starts; an
already-started finite Job episode may continue beside it.

Foreground/current Job, Task, and goal APIs remain compatibility and inspection
views rather than execution authority. Exact JobRun, Task, ActiveGoal,
continuation, progress, and Workspace authority remain context-local throughout
an asynchronous episode. Trigger activation visits enabled subscribers in
stable Job-ID order and applies ordinary admission to each. Pending activation
is scoped to the Job and trigger; explicit priority, fairness, preemption, and
durable activation queuing remain deferred.

Budget exhaustion leaves the Task and JobRun running in `awaiting_operator`;
an explicit `job work` can grant a fresh burst. An automatic provider failure
also moves to `awaiting_operator` and is not automatically retried. Terminal
Job operations clear the record.

An error while offering one context is isolated from other contexts and the
heartbeat. A failed pre-execution authority read leaves the exact parked binding,
readiness, progress, and automatic budget unchanged. A later ordinary opportunity
rechecks current authority and admission rules; stopping the Run or disabling the
Job still prevents work. The first error in each consecutive offer failure streak
is logged with a traceback. There is no immediate retry loop or retry queue, and
cancellation still terminates polling.

Continuation is session-local, volatile, bounded,
operator-fair, and non-recovering. The heartbeat neither scans durable running
rows nor discovers or starts enabled Jobs. Restart does not resume work, and
there are no execution claims, multi-runtime adoption, or persistent
continuation records.

### Continuation readiness

State-tending Jobs are validated against current runtime authority at outcome
acceptance, not against the event that originally activated the run. An enabled
`power_attention_required` trigger reads `RuntimeState.power.condition`; enabled
thermal and memory triggers read the PlatformMonitor's current hysteresis
classifications. Such a Job cannot successfully complete while any configured
authoritative condition remains active. Each condition remains independent, so
clearing thermal warning does not establish memory-pressure recovery on a Job
configured for both.

When progress depends on an independently observable recovery transition,
`wait_for_event` is preferred. `after_delay` is for cases where elapsed time itself
creates a justified reason to reassess, such as settling after an applied runtime
effect; it is not a substitute for a known recovery event. In particular, asking the
operator to connect power does not change power state and therefore requires
`wait_for_event(power_recovered)`, not `after_delay(30)` followed by asking again.
Read-only inspection and unknown effects likewise do not justify timed polling.
State-tending delayed reassessment requires an applied effect in the runtime's small
positive time-dependent-effect catalog. That catalog is currently empty because no
implemented Job-work effect changes a tended power, thermal, or memory condition;
future capabilities may opt in only with implemented and tested settling semantics.

An invalid completion, mismatched recovery event, or unjustified delay is rejected
through the outcome tool with bounded runtime-authored correction evidence. Cognition
may make one corrected proposal in the same bounded evaluation. If no valid proposal
is accepted, the fail-closed outcome parks the exact running JobRun and paused Task in
an `awaiting_operator` continuation. It schedules neither a timer nor an invented
recovery event, so an invalid completion is not terminalized, invalid polling is not
scheduled, and the active execution slot remains available to unrelated Jobs.

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
The bounded catalog contains `presence_changed`, `power_attention_required`,
`power_recovered`, `thermal_warning_raised`, `thermal_warning_cleared`,
`memory_pressure_raised`, and `memory_pressure_cleared`, mapped by the harness to
their semantic runtime events; model
output cannot name Python classes or provide predicates.
The event must arrive after the exact wait is armed. A matching event satisfies
the current wake and immediately offers continuation through the same ordinary
gate used by the heartbeat. That gate still decides whether work starts based on
the exact occurrence binding, lifecycle, Task state, authority and budget,
operator priority, attention, and active-work ownership. A waiting occurrence stays
durably `RUNNING`, but its exact volatile Job/Run/Task continuation is parked and its
Task is paused, so it does not own a Job cognition slot. An unrelated eligible Job
may use any free bounded slot. Event satisfaction
does not itself run cognition or consume budget. The periodic heartbeat remains
the fallback for reconciliation and retry after temporary contention. The
projected `present` boolean is a bounded runtime-authored fact; it establishes
reported presence only, not a person's identity, object visibility, safety, or
unrelated mutable conditions. Fresh acquisition remains necessary for facts
outside that payload.

Event satisfaction remains sticky through operator fairness, busy attention, and an
unrelated active Job. The one bounded wake is retained until the exact parked binding
can be restored; matching events coalesce rather than queue. Acceptance through
the ordinary continuation gate consumes the one wake context even if the
provider then fails. If that episode selects `wait_for_event` again, a new later
event is required. Events arriving while a bounded Job-work episode is active
are ignored rather than queued for a future wait. Explicit `job work` may
instead supersede either a satisfied or unsatisfied event wait; it receives
semantic continuity but no invented wake event.

Readiness is volatile, belongs to one exact JobRun/Task binding, and is removed
on terminal state, shutdown, or restart. A recurring schedule starts a new
occurrence; readiness only gates another episode of that same occurrence.
Events never discover or create JobRuns, and no selector, payload, subscription,
or event history is persisted.

`wait_for_operator` is reserved for new operator information or authority the runtime
cannot independently observe: an answer, decision, approval, missing parameter, or
operator-supplied fact. Physical involvement alone is not sufficient. When an
external condition is represented by a supported semantic event, use
`wait_for_event` even if the operator must cause it—for example, asking someone to
connect power uses `wait_for_event(power_recovered)`. `job work` explicitly restores
the sole parked occurrence when the execution slot is free; unrelated conversation
does not satisfy operator readiness.

Before restoration, the runtime re-reads the Job and JobRun and requires the Job to
still exist and be enabled, the Run to still exist in `running`, and every
Job/Run/Task binding and any continuation identifier to match. A transient authority
read failure leaves the exact parked context and valid grant unchanged. Definitive
rejection revokes the automatic grant without running cognition. A disabled or
missing Job leaves its known nonterminal Run's paused Task, exact identity, and
progress available for terminal administration and shutdown reconciliation. An
absent Run, mismatched durable ownership, or already-terminal durable Run permits
detaching that stale context; an immutable terminal result is not rewritten. A stale
Task binding likewise rejects execution while retaining the Run for reconciliation.
The runtime does not scan durable runs for replacement work.

Re-enabling after rejected restoration leaves retained work parked with no automatic
grant. Explicit `job work` or Task resume revalidates current authority; resume alone
does not rearm the revoked grant. Only an accepted explicit work outcome may grant
a fresh automatic budget. The old occurrence must be terminalized before another
occurrence of the same Job can start.

There is deliberately only one parked-continuation slot. If a second occurrence asks
to continue while that slot is occupied, the original parked authority is preserved
and the second occurrence becomes `failed` with the runtime-authored summary
`Continuation capacity unavailable: parked slot occupied`. `failed` is used because
the occurrence attempted bounded work but the runtime cannot safely preserve the
requested continuation; leaving it `running` would falsely imply resumable authority.

Power transitions log publication as `[POWER] event=power_attention_required
voltage_v=<v> status=published` or `[POWER] event=power_recovered voltage_v=<v>
status=published`. Job wake logs distinguish `action=matched` from
`action=retained_busy`.

### Semantic continuity

A continuing Job occurrence may carry the latest work summary into its next
bounded episode. This projection comes directly from the volatile
`JobContinuation.last_summary`, is limited to 750 characters, and is scoped to
the exact Job ID, JobRun ID, and Task UUID. It exists only to preserve immediate
progress context. It is not authoritative evidence about the current world or
runtime state and cannot independently establish a mutable condition or a
terminal Job outcome. It is distinct from runtime-owned `JobProgress`, which
may establish already-earned bounded progress in this occurrence. Current
acquisitions and effects remain authoritative for their current-episode facts.

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

A later episode may rely on committed exact-occurrence `JobProgress` when
deciding whether the TaskGoal is already satisfied; it need not repeat the
original side effect merely to recreate current-episode evidence. The model
must interpret a counter's name and value against the Job assignment, TaskGoal,
and other current evidence: the runtime does not equate arbitrary nonzero or
unrelated counters with completion. A rejected later attempt does not erase
earlier committed progress, nor does that rejection become success evidence.

Committed progress is cross-episode occurrence evidence, not a new update
basis. Only accepted evidence in the current episode can earn another
increment. Progress does not recreate its original evidence payload, establish
unrelated current-world facts, become persistent memory, or survive restart.
Exact Job ID, JobRun ID, and Task UUID binding remains fail-closed, and terminal
state clears the snapshot.

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

## Daily and hourly wall-clock activation

Each Job has at most one durable schedule configuration. The console grammar is:

```text
job schedule JOB<n>
job schedule JOB<n> daily HH:MM [--timezone ZONE]
job schedule JOB<n> hourly [--minute M] [--timezone ZONE]
job unschedule JOB<n>
```

Daily times use strict 24-hour `HH:MM`. Hourly minutes are integers from 0 through
59 and default to 0. Omitted timezone uses the application's configured timezone;
explicit zones must be valid IANA names, such as `America/Toronto`. Duplicate,
conflicting, unknown, and incomplete arguments are rejected. Inspection shows
cadence, local time or minute, timezone, enablement, and durable activation markers.
A successful setting acknowledges a persisted configuration; persistence failures
report failure. Administration and eligibility polling invoke no cognition.

Hourly means each civil hour at the configured minute in that timezone: `:45`
means 08:45, 09:45, and so on, irrespective of when the previous work finished.
It is not an elapsed 60-minute interval. Each hourly slot is identified by its
UTC instant, so repeated local times cannot collide. Non-whole-hour zones, such
as `Asia/Kathmandu`, still use the configured **local** minute.

Rollbacks can repeat more than one local hour, as in `Antarctica/Troll`. Slot
selection includes later local clock labels that already occurred before the
rollback and chooses the latest due UTC instant, even if the current local
clock reads earlier than that label.

| Situation | Hourly behavior |
| --- | --- |
| Startup/restart | Consider only the latest due slot, including the previous hour if the current hour's minute is still ahead. An unconsumed slot may start immediately. |
| Missed polls or downtime | Coalesce to the latest due slot; never replay a backlog. |
| Capacity, operator, unrelated Task/goal, or attention contention | Defer without consuming. Retry eligibility while that slot remains latest; replace it with the newer slot when one becomes due. |
| Same Job already live | Defer while its pending/running occurrence exists, including paused or parked work. A parked occurrence frees work capacity for other Jobs, but remains live for its own Job. |
| Forward wall-clock change | Consider the latest due slot at the new time, skipping intervening hours. |
| Backward wall-clock change | Admit only instants later than the durable hourly high-water mark. Previously consumed or older instants cannot replay, even after restart. |
| Failure, stop, interruption | The admitted slot remains consumed. Do not automatically retry that occurrence; a later slot can create a new occurrence after the Job is no longer live. |
| Toronto fall-back | Both instances of a repeated hour are distinct instants and may each run once. If both were missed, only the latest due one is considered. |
| Toronto spring-forward | Nonexistent local times create no slot. The last real due slot remains current until the next real slot becomes due. |

Daily behavior remains calendar based: a time is due at its exact minute or later
on that local date. A restart at 08:00 can catch up a missed 02:00 occurrence,
but never backfills older dates. `last_started_local_date` permits one scheduled
occurrence on that local date. Daily DST behavior is unchanged: a repeated hour
runs once per local date; a nonexistent daily time becomes eligible after the
clock passes the gap. Daily deduplication retains the existing last-date policy,
rather than the hourly monotonic instant policy.

Setting a schedule replaces the Job's previous configuration, including cadence,
time/minute, and timezone; it never adds another schedule. The new configuration
is eligible at the next poll. Replacement does not terminate existing work.
Daily date markers and hourly instant markers survive replacement and cadence
changes. For example, daily-to-hourly may admit the latest hourly slot if no
hourly high-water mark prevents it; hourly-to-daily still honors a previously
consumed daily date. Changing an hourly minute backwards cannot admit a slot
at or before its hourly high-water mark. Removing a schedule prevents future
scheduled admission and preserves Job history and live work. The hourly
high-water mark survives removal/recreation as well, preventing same-slot replay;
daily removal retains its existing behavior of removing the daily date marker.

At each lightweight opportunity, the application orders latest due slots by
scheduled instant and Job ID and admits at most one occurrence. Disabled Jobs or
schedules and Jobs with live occurrences are skipped, allowing an independent
Job to proceed. Runtime-wide blockers defer that opportunity. The store uses an
immediate SQLite transaction to revalidate the selected schedule snapshot, Job
enablement, slot eligibility, and absence of pending/running runs, then consumes
the marker and creates the pending JobRun atomically. Update, insert, and commit
failure roll back both. Scheduled, trigger, and explicit runtime admissions make
the same-Job non-overlap decision under the store's write lock. Existing Task,
goal, resource, lifecycle, capacity, and operator ownership checks still apply.

Scheduled work binds an ordinary Task, claims the existing autonomous attention
lane, and enters the existing bounded Job executor. Recurring schedules create
**new occurrences**; continuation/readiness only advances an **existing
occurrence**. A `continue` outcome uses the existing continuation policy and does
not establish a recurring schedule. Scheduler polling frequency only controls
when eligibility is checked; it changes neither cadence nor work budgets and
does not itself invoke cognition. There is no new execution loop or cognition
tool, general cron grammar, multiple schedule list, or arbitrary interval support.

The timer checks immediately at startup before its first poll sleep. Check
exceptions are logged and followed by the normal sleep. Shutdown stops the timer
before cancelling work. Startup reconciliation interrupts orphaned pending/running
runs before activation; it preserves both durable schedule markers and never
adopts, resumes, or retries the interrupted occurrence. Cross-runtime execution
leases and multi-body eligibility remain future work: the SQLite admission lock
does not make concurrent application startup/reconciliation safe against another
live runtime using the same store.

Post-merge operator examples (configuration only; these have not been run against
a deployment):

```text
job schedule JOB16 hourly --minute 45 --timezone America/Toronto
job schedule JOB16
job schedule JOB16 daily 12:45 --timezone America/Toronto
job unschedule JOB16
```

These examples do not change a Job's description, responsibilities, completion
criteria, acquisition/effect budgets, cognition, triggers, or carry-forward policy.
JOB4 and the nightly reviewer are outside this experiment; no deployed Job
configuration is changed by this implementation or its migration.

While a Task is paused, its JobRun remains `running`. Existing Task behavior
releases Task-owned resources and suspends its ActiveGoal; resume creates a
fresh Task-owned ActiveGoal without reacquiring resources. Completion, failure,
and stop map to matching Task and JobRun terminal statuses. Completed and
stopped summaries are outcomes; failed summaries are errors.

Task terminalization precedes durable JobRun terminalization. If persistence
then fails, the terminal Task is not restarted: the volatile binding is retained
fail-closed so the same terminal intent can be retried, while a conflicting
terminal retry is rejected. Exact Run-ID retry validates the target's retained
terminal Task snapshot, released Task activation binding, and durable ownership;
an unrelated live Run's foreground or active Task does not block or authorize it.
Retry persists the decision without replaying cognition or effects. Already-terminal
durable results remain immutable under the store's existing transition rules.

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

Jobs use their own `SQLiteJobStore`, schema version 12, normally in
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

`interrupted` is a terminal JobRun state distinct from `completed`, `failed`,
and the policy-driven `stopped` state. It records loss of execution continuity;
it is neither successful work nor a promise that work can be resumed. An
interrupted occurrence has `finished_at`, needs no result report, remains
visible in JobRun history, and is excluded from latest-completed-result lookup.

Shutdown first stops the continuation heartbeat, cancels and joins in-flight
Job work, and clears continuation. Each known durable occurrence still in
`pending` or `running` after that quiescence, including retained work whose
restoration was rejected, is marked `interrupted`
without cognition, a result report, or a model-authored summary. It then
releases the current Task's volatile ActiveGoal and resources and clears both
Task and JobRun bindings before closing the store. A legitimate terminal result
persisted while work was quiescing is not overwritten.

At startup, before scheduled activation, continuation processing,
`ApplicationStarted`, or normal execution, the store atomically changes every
stale durable `pending` or `running` occurrence to `interrupted`. This creates no
Task, ActiveGoal, continuation, or automatic work and does not resume or retry
the old occurrence. For this crash-reconciliation case, `finished_at` is the
time the new process detected and durably reconciled the orphan, not an inferred
crash time. Startup fails closed if the transaction cannot complete.

The reconciliation preserves Job definitions, schedules, daily
`last_started_local_date`, and hourly `last_started_slot`. Consequently, an
interrupted scheduled occurrence is not recreated in its consumed daily date or
hourly slot; a later eligible slot can start normally.
Durable Workspace artifacts published before interruption survive independently,
but their existence does not turn the interrupted JobRun into completed work.
Automatic JobRun recovery is not implemented.

The console inspection commands are `jobs`, `job show JOB<n>`, `job runs
JOB<n>`, `job files JOB<n> [directory]`, and `job file JOB<n> <logical-path>
[offset_chars]`. Definition commands are `job add`, `job update`, `job enable`,
and `job disable`. `job update JOB<n> --description ...` changes the durable
responsibility definition in place for future occurrences while preserving Job
identity, history, schedule, and Workspace. The currently executing same Job
cannot be updated until its occurrence is terminal.
Runtime coordination commands are `job start`, asynchronous `job work`, `job
current`, `job complete`, `job fail`, and `job stop`. Terminal commands accept
an exact `RUN<n>` selector; selector-less compatibility is allowed only when one
live occurrence is unambiguous. Exact control cancels and joins that Run's active
cognition before terminalizing its Task and durable result. Disabling a Job
prevents future activation but deliberately does not stop an already-live Run.
Rejected restoration retains a known nonterminal Run for exact terminal control;
it does not silently stop it or discard its last usable owner.
Exact stop, complete, and fail decisions may also terminalize a parked Run; the
runtime does not start autonomous cognition merely to apply that operator decision.

It does not mark the durable run failed; process shutdown or later startup
reconciliation marks an unfinished occurrence `interrupted`. Cron, new-Job
scheduling, Job-owned resources, unbounded retries, body/runtime registries,
target matching, distributed coordination, claims, and restart recovery remain
future work.

Each durable Job also owns one durable contained text Workspace shared across
its occurrences. It remains separate from immutable JobRun result reports and
from all volatile coordination state. See [Job Workspaces](job-workspaces.md).

## Event-triggered responsibilities

A `JobTrigger` is durable configuration from a bounded runtime-owned catalog,
separate from the unchanged daily `JobSchedule`. Supported triggers are
`power_attention_required`, `thermal_warning_raised`,
`memory_pressure_raised`, and the lifecycle occurrence `runtime_ready`;
operators configure one with `job trigger JOB<n> <event_type>` and remove it
with `job untrigger`. A matching event starts the ordinary JobRun → Task →
attention lifecycle only for an enabled Job.

For example, `job trigger JOB13 runtime_ready` durably binds that Job to the
readiness of a future runtime instance. `runtime_ready` occurs once after a
successful application startup has completed its configured service attempts
and normal controllers are usable. It describes the current composed runtime
and embodiment, not an OS boot, recurring schedule, or state-tending condition.
It uses the same trigger admission, attention arbitration, bounded work, and
result persistence as every other Job occurrence. A process restart creates a
new runtime instance and therefore may legitimately create another occurrence.

Several Jobs may own enabled bindings for the same event type. One event is
offered independently to those subscribers in ascending Job ID order, and one
subscriber's ineligibility or activation failure does not suppress the others.
A raised semantic event may simultaneously satisfy existing continuation
readiness and offer new trigger activation to subscribed Jobs. Same-Job
active-occurrence coalescing prevents that dual use from creating a duplicate Run.
Multiple trigger types may point to the same durable responsibility. Only one active
occurrence of that Job is admitted regardless of which configured trigger fires.
Further matching events coalesce rather than forming a queue. Runtime readiness
also has an application-lifetime observation guard, so accidental duplicate
delivery cannot create another startup occurrence. If coordination is
busy, including while operator attention is active or waiting, the application
retains one volatile pending bit per `(job_id, trigger_type)` subscription and
retries it at the next lightweight Job activation opportunity. Repeated events
coalesce, so this is bounded by configured subscriptions rather than raw events.
Capacity admits as many initial episodes as the existing Job work slots allow.
Pending state is discarded on shutdown, trigger removal, or Job disablement and
never stores event history or payloads. If its continuation is waiting for that
semantic event, the same run wakes. `wait_for_event` supports
`presence_changed`, `power_attention_required`, `power_recovered`,
`thermal_warning_raised`, `thermal_warning_cleared`, `memory_pressure_raised`,
and `memory_pressure_cleared`. A wake
payload proves only the named transition and bounded transition value. Cognition
must consult current `RuntimeState` or acquire fresh evidence for other facts.
For power specifically, `PowerRecovered` discards a pending attention activation,
and admission rechecks authoritative `PowerState.condition` so resolved historical
transitions cannot start cognition later.
Thermal and memory-pressure admission follows the same fail-closed rule: a deferred
activation is admitted only while the `PlatformMonitor`'s current hysteretic condition
remains active. Cleared events proactively discard pending bits, while this current-state
check also protects retry ordering without treating the earlier event payload as authority.

Once normal event and attention coordination is ready, startup takes a fresh platform
snapshot, installs it as current `RuntimeState`, and re-establishes the monitor baseline
from that same observation. Earlier initialization-time samples are diagnostics, not
semantic transition history. The monitor publishes at most one semantic raised event
for each condition active at this operational handoff. Its retained hysteresis state
prevents the first periodic sample from publishing the same transition again. Normal
handoff conditions publish nothing, and simultaneous health conditions still pass
through ordinary per-Job active-occurrence coalescing rather than creating a backlog.

The complete boundary is: a sensor measurement is cheaply classified into
authoritative `PowerState`; a semantic transition event selects configured Job
authority; a new JobRun performs bounded cognition; and a later event may wake
that same run. Interrupted runs are never resurrected. Startup attention creates
a new occurrence when the condition remains unresolved.

The Jobs database schema version is 12. Opening a version-4 database transactionally
adds the trigger table; opening version 5 expands its bounded event-type constraint.
Opening version 7 removes the former globally unique enabled-event index without
rewriting or deleting trigger rows. All migrations preserve Jobs, JobRuns,
schedules, result reports, and trigger rows. Version 11 extends the single
schedule configuration with daily/hourly cadence and hourly minute and adds a
per-Job hourly activation high-water mark. Existing schedules migrate as daily,
with their date markers intact. IDs, results, Findings, triggers, and focus are
preserved; migration performs no activation. Unchanged daily schedules keep
their legacy focus identity. Hourly cadence, minute, timezone, and schedule
enablement participate in definition identity; activation markers do not.

## Session execution contexts

Every live occurrence is represented by one runtime-owned
`JobExecutionContext`. Its stable identity is the exact `(job_id, run_id,
task_uuid)` tuple; a context is never rebound to a later run of the same Job.
It owns volatile execution authority: the Task/TaskGoal and Job ActiveGoal
relationship, continuation and readiness/wake state, progress, active asyncio
work task, parked/runnable state, Workspace scope, and Task resource-owner
identity. Terminalization removes only that occurrence's context; shutdown
cancels and joins context-owned tasks, interrupts durable running JobRuns under
the existing policy, releases Task resources, and clears the volatile registry.

`current_job_run`, `current_task`, `active_goal`, `job current`, and runtime
inspection remain compatibility projections of the foreground context (or the
sole parked occurrence where existing behavior requires it). They are views,
not independent mutable owners. A parked context retains the exact paused Task
and continuation but owns no active attention episode, provider request, or
physical-resource lease.

Within Job work, ordinary acquisition and effect projection validates the exact
context's running Task/ActiveGoal binding. It does not depend on a global goal
left by another Job: completing or parking that other occurrence must not remove
inspection tools from valid work. Conversely, a stopping, parked, terminal or stale
context cannot borrow a sibling's global goal to obtain tools. Existing initiative
permissions, episode budgets and capability-specific execution checks still apply.

The context registry is the authority for bounded Phase-3A Job-vs-Job cognition
concurrency. Runnable and parked contexts consume no work slot until their exact
asyncio cognition task starts. The operator lane remains independent, while
general autonomous initiative remains conservative and separate. Shared
WorkingMemory and persistent memory remain application-level sources; contexts
neither contain nor create private conversation histories or semantic-memory
stores. Workspace authority remains captured from the exact Job context for each
bounded cognition episode. Phase 3B-2 priority, fairness, preemption, and durable
activation queuing remain deferred; subscriber ordering is deterministic admission
iteration, not priority.

## Request provenance

With conversation history enabled, an explicit console `job add` interaction links
its request reference to the created definition; `job start` links a distinct
request to the exact newly created JobRun. Later scheduled or triggered occurrences
are never inferred to belong to the definition-creation request. Operator cognition's
`discover_requests` can find older interactions by explicit Job/Run links or bounded
pagination, and console Job responses print their request references. `inspect_request`
can follow these durable links to exact status and retained-result selectors, including after restart. Reports remain in the Jobs database. Missing or
interrupted work is not resumed, and links grant no new authority. See
[conversation continuity](conversation-history.md) for identity, retention, storage
failure limits, and the bounded operator-only inspection tools.

## Opt-in procedure learning

Private bounded methods and the explicit disabled, unscheduled experimental Job
are documented in [Job procedures](job-procedures.md). Schema 12 adds default-off
procedure policy and bounded per-Run snapshots without changing existing Jobs.
