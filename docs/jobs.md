# Jobs

Jobs are durable runtime infrastructure. Phase 1 records responsibilities and
their occurrences; it deliberately does not execute, schedule, route, or retry
them.

## Job

A **Job** is a durable responsibility with an integer catalog identity (shown
as `JOB1` in the console), name, description, enabled flag, optional target,
and creation/update timestamps. Enabled means the responsibility may be
considered by a future execution policy; disabled means automatic execution
must not consider it. Neither state causes execution in Phase 1. Job
definitions cannot be deleted through the Phase 1 store API and are retained.

A `JobTarget` is an open-ended pair of validated identifiers, such as
`body:camera`, `agent:mira`, or `runtime:workshop-controller`. It is assignment
metadata, not proof that a reader is eligible or authorized to execute the Job.
Targets need not refer to currently attached hardware.

> `target=None` means unassigned, not execute everywhere.

No Job kind/handler key is included yet: Phase 1 has no dispatch behavior, so
such a key would have no current invariant. Dispatch semantics belong to a
future execution phase.

## JobRun

A **JobRun** is one durable occurrence or attempt, with its own integer
identity (shown as `RUN1`), a foreign key to its Job, status and timestamps,
and optional bounded plain-text outcome/error summaries. Its lifecycle is:

```text
pending -> running -> completed
                   -> failed
                   -> stopped
pending ----------------> stopped
```

Completed, failed, and stopped runs are terminal. Transitions are atomic and
fail closed. Closing a store or application does not rewrite pending or
running runs; crash recovery is deliberately not inferred.

## Task

A **Task** is bounded meaningful work. A later execution phase may create or
coordinate Tasks for a JobRun, but Phase 1 adds no such relationship:

```text
Job -> JobRun -> future Task -> actions/resources
```

## Catalogs, assignment, and claims

One Jobs SQLite database is one globally readable catalog. `list_jobs()` lists
the whole catalog unless its caller explicitly supplies a target filter. It
does not inspect `RobotProfile`, attached hardware, or the current process.

> A Job database may contain responsibilities assigned to multiple bodies or
> runtimes. Reading the catalog does not imply authority or eligibility to
> execute every Job.

For example, one logical Mira catalog can contain all of these:

```text
JOB1  body:camera   Keep subjects in frame
JOB2  body:arms     Hold workpiece
JOB3  body:sprayer  Paint workpiece
JOB4  agent:mira    Nightly Self Log Reviewer
```

A coordinator can inspect all four. A future camera runtime may determine that
it is eligible for `JOB1`, but readability alone says nothing about `JOB2` or
`JOB3`. The three concepts remain separate:

1. **catalog visibility** — every Job in the store is readable;
2. **execution assignment** — the optional target records intent; and
3. **runtime execution claim** — future transactional ownership of one run.

If multiple processes share a catalog, merely matching the same target must
never be enough for both to start the same run. Automatic execution will need
an atomic claim/lease with a runtime identity. Phase 1 intentionally has no
`claimed_by` column because there is no claim behavior or invariant yet; the
focused `job_runs` table can be migrated to add that policy later.

## Persistence and configuration

Jobs use their own `SQLiteJobStore` and schema version, normally in
`data/jobs.sqlite3`. They are not tables in `SQLiteMemoryStore`: memories and
Jobs have independent purposes, lifecycles, enablement, and schema evolution.

```toml
[jobs]
enabled = true
database_path = "data/jobs.sqlite3"
```

Jobs and persistent memory can each be enabled or disabled independently. The
console commands `jobs`, `job show JOB<n>`, and `job runs JOB<n>` inspect the
catalog and clearly render a missing target as `unassigned`. They do not create
or execute work.

Scheduling, timers, execution, Task creation, resource acquisition, retries,
body/runtime registries, target matching, distributed coordination, claiming,
and crash recovery are explicitly deferred.
