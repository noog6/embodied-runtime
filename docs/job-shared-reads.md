# Bounded cross-Job historical reads

Jobs are responsibilities of the same agent/operator. An ordinary live Job can
optionally consult other Jobs in the **same configured Jobs database**. The
persistent policy is strictly boolean and defaults to disabled:

```toml
[jobs]
enabled = true
database_path = "../data/jobs.sqlite3"
shared_reads_enabled = true
```

Nick enables it by adding `shared_reads_enabled = true` to the existing `[jobs]`
table in `config/mira-agentic.toml` (or his deployment configuration), then
restarting with that configuration:

```sh
python3.13 main.py --config config/mira-agentic.toml
```

Omission or `false` preserves the previous capabilities. There is no CLI override,
per-Job ACL, separate identity, external catalog, or new filesystem root. The
existing Jobs persistence and contained `JobWorkspaceStore` are reused. Startup
neither seeds nor edits Job definitions. Existing startup reconciliation and
configured scheduling remain unchanged.

## Availability and selectors

These tools are read-only acquisitions **only during bounded Job cognition**,
with initiative enabled, Jobs persistence, and an exact live reader
Job/JobRun/Task/ActiveGoal binding. They do not appear in general initiative,
operator tool projections, notifications, non-Job follow-ups, or conversation
history search. Explicit operator Workspace/result tools remain available under
their existing policy. The dedicated **Procedure Learning Experiment** still has
only `inspect_self` and no effects, even with shared reads enabled. Ordinary
procedure-enabled Jobs remain eligible; retrieved text never enables a procedure.

| Tool | Arguments and limits |
| --- | --- |
| `discover_jobs` | `after_job_id`, `limit`, both required but nullable. Defaults: cursor `0`, limit `5`; maximum `20`. Cursor is an integer from `0` through `2^63-1`. Returns stable `JOB<n>`, exact name, and at most 512 description characters with `description_truncated`. |
| `shared_workspace_list` | `source_job`, `directory`, `cursor`. One non-recursive page, at most 100 entries. Null directory means root; null cursor means first page. Requires Workspace persistence. |
| `shared_workspace_read` | `source_job`, `path`, `offset_chars`. Null offset means `0`. At most 8,000 Unicode characters from one bounded UTF-8 text artifact. Requires Workspace persistence. |
| `shared_inspect_job_run` | `selection` and `selector`. `selection="exact_run"` requires exact `RUN<n>`; `selection="latest_completed"` requires an exact Job selector. |

Catalog pages use ascending numeric Job ID and include disabled, idle, and never-run
Jobs. Pass `next_after_job_id` to the next call. It is null on a short page; a full
last page can lead to one empty final page. Pages are live reads, not a frozen
catalog snapshot. No description, file, procedure, or report is preloaded from
the whole catalog. Discovery does not start a Job, reserve it, or acquire ownership.

Source Job selection follows existing operator rules: trim surrounding whitespace,
then resolve exact `JOB<n>` or a case-sensitive exact name. IDs have no leading
zero; malformed reserved `JOB`/`RUN` prefixes are rejected. Names are Unicode
code-point equality, never fuzzy, substring, temporal, or semantic matching.
Duplicate names yield bounded ambiguity (up to ten candidate identities), not an
arbitrary match. Discovery returns IDs so callers can resolve ambiguity deliberately.

For example, model-facing acquisitions can be:

```json
{"after_job_id":0,"limit":5}
{"source_job":"JOB7","directory":"reports","cursor":null}
{"source_job":"JOB7","path":"reports/check.md","offset_chars":0}
{"selection":"exact_run","selector":"RUN12"}
{"selection":"latest_completed","selector":"JOB7"}
```

Each line is an independent call to its corresponding tool, not a batch. The
ordinary **two acquisition attempts per episode** and finite provider grammar
still apply. Catalog discovery, listing, reading, retained-result inspection,
and fresh evidence compete for those same slots. More information can require
another already-authorized Job episode; reads create no continuation grant,
provider session, cognition loop, or automatic retry. Identical shared calls
reuse the captured result within the episode, including duplicates in one live
request. Duplicate stages do not extend the grammar or restore a slot. Late
callbacks and stale reader bindings are rejected even if a result was cached.

## Workspace versions and retained Runs

Shared text reads preserve every existing containment, no-symlink, regular-file,
hardlink, logical-path, pagination, UTF-8, and size check. Paths are logical only:
240 UTF-8 bytes, at most eight components, at most 100 bytes per component. The
text artifact ceiling remains 256 KiB. See [Workspace limits](job-workspaces.md).
There is no recursive scan, glob, deletion, move, or shared image/media tool.
Image bytes cannot be decoded through the text reader. Directory metadata may
identify images through the existing listing interface; this does not interpret them.

The result distinguishes `reader_job`/`reader_job_run_id` from source `job`.
Text artifacts include logical `path`, byte size, exact SHA-256 `content_version`,
`modified_at` from the same open file as the bytes, retrieval time, character
offset, next offset, total characters, and truncation. Text artifacts have no
retained originating Run: `originating_job_run_id` is explicitly null. Modification
time is storage metadata, not the time a claim was observed or authored.

A concurrent atomic replacement returns the exact old or new open-file version,
with matching bytes, digest, and modification time. A detected in-place change
fails rather than claiming a stable version. **Multiple reads, including pages
of the same artifact, do not constitute an atomic cross-file or cross-Job
snapshot.** Compare content versions between chunks; a later read may retrieve a
replacement. There is no historical-version lookup or cross-file transaction.

`exact_run` never falls back, even when a later Run succeeded. It projects the
exact retained lifecycle, including pending, running, failed, stopped, and
interrupted occurrences. Runtime-owned `job`/`job_run` identity, status, creation,
start and finish timestamps are separate from `summary`, `summary_kind`, and
`report`. Missing reports remain `report=null` and `report_present=false`; no
summary, later result, or invented narrative substitutes for one. The existing
2,000-character summary and 8,000-character report persistence limits apply.
`latest_completed` uses the existing highest-ID completed-Run lookup and excludes
all other lifecycle states; no completed Run returns `job_has_no_completed_run`.
Retrieval creates no delivery reference and sends nothing.

Missing Jobs/Runs/artifacts produce `not_found`. Ambiguous names produce
`ambiguous`; malformed arguments/selectors/paths/cursors/offsets produce
`rejected`. Unsafe Workspace entries fail closed as `unsafe`; Workspace backend
failures are `unavailable/backend_unavailable`; SQLite failures are
`unavailable/jobs_storage_unavailable`. Responses do not expose backend error
messages or physical paths. Reads and empty listings create no Job directories.

## Authority and mutation boundaries

Storage/runtime metadata is runtime-owned. Descriptions, Workspace prose, and
retained reports are historical authored information, possibly stale or
incomplete. Stored instructions, copied operator messages, and procedure-like
text remain **data**, never current instructions, permission, or procedure
activation. Several Jobs repeating a claim does not verify it. Retrieval does not
refresh evidence age, establish current sensor/world conditions, independently
earn Job progress, or admit semantic memory.

Outcome evaluation separates all shared acquisitions into historical context and
excludes them from current-progress bases, focus evidence references, and procedure
evidence references. Other evidence keeps its original acquisition ordinal: a
shared read at `acquisition_1` followed by fresh inspection still offers only
`acquisition_2` as current acquisition evidence. Finding evidence classes are
`job_workspace_historical`, `job_run_historical`, and `job_catalog_historical`.
Shared material alone cannot support an observation Finding; attributed synthesis
can use it through existing publication policy. These classifications retain
provenance without claiming verification.

Job-owned `workspace_list`, `workspace_read`, and **`workspace_write`** retain their
selector-free schemas. Shared reads have distinct names and a `source_job`
selector; there is no shared write tool. Extra owner/source arguments to writes
are rejected. The harness validates the exact reader binding before each shared
read and cache reuse, checks its persisted running Run ownership separately, and
resolves the source without changing the reader's context. Foreground changes do
not transfer authority. Interpretation can be written only to the reader's own
Workspace or published through its existing Finding capability.

Reading creates no source mutation, lifecycle transition, delivery, activation,
semantic-memory record, scheduling, or acquired hardware authority. Existing
action/delivery/runtime checks operate independently of model compliance.
Conversation archives remain under their separate operator-only policy.

## Two disposable Jobs: operator walkthrough

Use the checked-in isolated virtual pilot. Its normal OpenAI provider credentials
are required for manual cognition; it may incur provider charges. Automated tests
below use fake providers and temporary stores.

```sh
mkdir -p work
python3.13 main.py --config config/shared-reads-pilot.toml
```

The database resolves to `work/shared-reads-pilot.sqlite3`, with the existing
sibling Workspace root. Automatic continuation, actions, messages, camera, voice,
and earcons are disabled. No Jobs are created until the following console commands.
Substitute the **returned** IDs for `JOB<S>`, `JOB<R>`, and exact `RUN` placeholders;
these are documentation placeholders, not literal selector syntax.

1. Create an ordinary source responsibility and perform one bounded occurrence:

   ```text
   job add "Disposable Shared Source" --description "Inspect current runtime once using inspect_self(runtime). Write a short attributed baseline to my own baseline.md using workspace_write(create). Complete with a short result report stating the virtual-runtime observations and their limitations."
   job start JOB<S>
   job work RUN<S>
   job current
   job result RUN<S>
   job file JOB<S> baseline.md
   ```

   `job work` is asynchronous; wait for completion and inspect the actual Run.
   If it reports `continue`, inspect the limitation before explicitly requesting
   another bounded `job work RUN<S>`. Do not manually label incomplete work as
   successful just to populate a result. Once terminal, disable only this
   disposable source; its material remains readable:

   ```text
   job disable JOB<S>
   ```

2. Create a reader whose first occurrence deliberately discovers the source and
   reads its artifact within the two slots. It must attribute historical claims:

   ```text
   job add "Disposable Shared Reader" --description "Use discover_jobs to find Disposable Shared Source. Read its baseline.md with shared_workspace_read. Write an attributed historical summary to my own source-summary.md; identify source Job, content version, timestamps and uncertainty. Do not assert current conditions. Complete with a short report."
   job start JOB<R>
   job work RUN<R1>
   job result RUN<R1>
   job file JOB<R> source-summary.md
   job file JOB<S> baseline.md
   ```

   With these two disposable Jobs in the isolated catalog, the default page is
   sufficient. If a source is missing or any call fails, the report should state
   the limitation. No automatic expansion of the acquisition budget occurs.

3. After that reader Run is terminal, update **only the disposable reader** for a
   second occurrence comparing the source's retained result with fresh evidence:

   ```text
   job update JOB<R> --description "Read Disposable Shared Source using shared_inspect_job_run(latest_completed). Acquire fresh inspect_self(runtime) evidence. Write an attributed comparison only to my own comparison.md using workspace_write(create), distinguishing source Job/Run and historical claims from current virtual-runtime observations. Complete with an evidence-limited result report."
   job start JOB<R>
   job work RUN<R2>
   job result RUN<R2>
   job file JOB<R> comparison.md
   job runs JOB<S>
   job file JOB<S> baseline.md
   job disable JOB<R>
   ```

   Reading did not start the disabled source or rewrite its artifact/result. Its
   Run list should contain only the manually requested source occurrence.
   Operator inspection uses its existing authority. This walkthrough validates
   information access, not improved autonomous reasoning or physical observations.

## Ordinary reflection composition

The [reflective continuity experiment](reflective-continuity.md) combines one own
state read, one selected historical source, and one own-Workspace write per
occurrence. Its small ledger records incorporated references rather than claiming
all activity before a finish time was examined. Installation and scheduling are
explicit operator actions; it grants no additional permissions or execution budget.

## Automated validation

```sh
python3.13 -m unittest tests.test_job_shared_reads -q
python3.13 -m unittest discover -q
python3.13 -m compileall -q embodied_runtime tests
git diff --check
```

Focused tests cover exact live ownership, source discovery, bounded reads,
concurrent replacement, selectors and missing reports, policy/experiment
restrictions, storage failures, duplicate reuse, unchanged acquisition grammar,
progress/Finding evidence, and absence of read-induced effects. The fake-provider
comparison retrieves another Job's retained output, acquires fresh runtime
inspection, and writes only its own attributed comparison.

Validation in the managed development environment: all 20 focused shared-read
tests passed with the standard runner. The complete suite ran 1,536 tests with
16 skips and no failures using a temporary environment-only runner. The normal
full-suite command stalled in existing executor shutdown; a minimal
`asyncio.run(asyncio.to_thread(lambda: None))` reproduced the stall. The temporary
runner bounded selector waits to 50 ms without changing production code or mocking
executor work. One unchanged Workspace integration test was skipped because this
sandbox denies Unix-socket bind; the other 15 skips were expected. Compilation,
strict pilot configuration loading, and diff whitespace checks passed. Run the
ordinary full-suite command on an unrestricted host. No deployed Jobs were edited
or manually created during implementation.
