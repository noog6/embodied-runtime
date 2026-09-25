# Run history

Each authoritative run directory also receives `summary.json` during process
finalization. This schema-versioned document contains run identity/timing/status,
aggregate operational counters, bounded dimensions, and a `provider_usage` array. Each
provider/model item contains `requests`, `input_tokens`, `cached_input_tokens`,
`cache_write_tokens`, `output_tokens`, `total_tokens`, and `duration_ms`, in addition to
the bounded provider and model identifiers. These are raw provider-reported usage totals.
The document also includes an explicitly derived cost result. It contains no prompts,
transcripts, credentials, media, or model responses. Summary writing is best effort and
cannot prevent
ordinary shutdown; `run.json` remains the authority for existing run-history status.

The `interruptions` counter is incremented once only when the top-level process result
is the authoritative interrupted exit code (130). Task cancellation and lower-level
cleanup do not increment it; completed and other failed runs leave it at zero.

Raw usage is authoritative. Monetary cost is `unavailable` (not zero) unless every
used provider/model has a rate in an explicitly identified `PricingCatalog`. Rates
independently cover input, cached-input, and output tokens and no network pricing lookup
is performed. Cached reads and cache writes are subtracted from total input before the
ordinary input rate is applied, so input categories are mutually exclusive. Structurally
inconsistent usage, a missing provider/model rate, or cache-write usage without a
cache-write rate makes the estimate unavailable. The OpenAI Responses adapter records
provider-reported cached reads and cache writes when present; absent detail fields are
recorded as zero. Normal provider request log lines likewise include
`cache_write_tokens=<n>` when that valid provider detail is present. Local speech
activity is not assigned hosted cost.

Summary persistence reports `written`, `failed`, or `not_requested` to the runtime. A
failure is logged with its bounded exception class and never changes authoritative
`run.json` status or blocks shutdown.

A **run** is one validated invocation that enters runtime execution. Help,
argument errors, and invalid launch combinations are not runs. By default each
run has a durable local directory under `data/runs/`:

```text
data/runs/R1/run.json
data/runs/R1/runtime.log
```

Run IDs are monotonically increasing local numbers (`R1`, `R2`, ...). The next
number is one greater than the highest existing direct `R<positive integer>`
directory; gaps and unrelated names are ignored, and an allocation collision
is retried at the following number. Existing runs are never overwritten.
Sequence is not time: a run may span midnight or any number of dates without
its ID changing.

## Metadata

`run.json` has schema version 1 and contains `run_id`, `run_number`,
`started_at`, `ended_at`, `status`, `exit_code`, `profile`, `hardware`, and
`config_source`. Timestamps are local ISO-8601 values with milliseconds and an
offset. A clean result is `completed`, exit code 130 is `interrupted`, and any
other explicit nonzero result is `failed`.

The initial record remains `started`, with null `ended_at` and `exit_code`, if
clean finalization was not recorded. This means only **unfinalized**; it does
not prove a crash or any particular cause. Metadata creation and finalization
use a flushed temporary sibling followed by atomic replacement of `run.json`.

Setup remains provisional until the `[RUN] ... status=started` record is
emitted. If metadata or log-handler setup fails before that boundary, the
runtime removes only the new run's owned metadata, temporary file, and log,
then removes the directory only when empty. It never recursively removes
unknown contents. A successfully removed provisional number may therefore be
reused; an authoritative unfinalized run is never rolled back.

## Log and privacy boundary

`runtime.log` is a plain, timestamped copy of records already emitted through
embodied-runtime's centralized Python logging. It has no ANSI colour and uses
the same transport-noise filtering as the live console. It is **not** a
complete stdout/stderr transcript: native and vendor output is not redirected
or captured.

History does not add conversation bodies, prompts, model request or response
bodies, reasoning, memory payloads, images, audio, transcripts, credentials,
or environment data. If history storage is unavailable, the runtime warns once
and continues; observability failure does not determine the application result.

## Read-only console browser (v2)

Schema v1 files remain the authoritative storage and write layer. The local
console merely derives bounded, read-only views from the same history root used
by the CLI; it neither repairs nor rewrites historical artifacts:

- `runs` lists the newest 20 direct run directories in descending numeric order
  and reports how many older directories were omitted. Invalid or unavailable
  metadata is shown as `unavailable` rather than crashing the browser.
- `run show R<n>` validates and displays every bounded schema-v1 field plus a
  duration. Finalized durations are truncated to whole seconds and formatted as
  `HH:MM:SS`, or `Nd HH:MM:SS` for durations of at least one day. A stored
  `started` run retains that label and a `-` duration; it does not imply either
  running or crashed.
- `run grep R<n> <text>` performs a case-insensitive literal substring search
  of `runtime.log`, includes original line contents and line numbers, and shows
  at most 50 matches plus a truncation notice. It is not regular-expression
  search.

Only exact `R<positive integer>` identities (case-insensitive at the console)
are accepted. Arbitrary paths are never accepted. Direct run-directory
symlinks and symlinked `run.json` or `runtime.log` files are not followed;
metadata reads are limited to 64 KiB. Missing, malformed, oversized, or unsafe
artifacts produce concise reports without changing any history file.

These commands are process-level operator administration. They do not enter
cognition, add WorkingMemory turns, expose run identity through self-inspection,
or alter RuntimeState or persistent semantic memory.

## Bounded cognition acquisition (v3)

Version 1 is the durable run record, version 2 is the local operator browser,
and version 3 is a deliberate read-only cognition acquisition over that same
evidence. When the CLI successfully creates a run, it injects a read-only
`RunHistoryEvidenceReader` with that exact `RunHistory.run_id`; it never gives
the application the writable history object. If creation fails, explicit IDs
and recent discovery remain usable but `current` and `previous` are unavailable.
Direct `RobotApplication` construction has no history provider by default.

`inspect_run_history(selector, query)` exposes two required fields. `selector`
accepts `recent`, `current`, `previous`, `previous_day`, or an exact case-insensitive
`R<positive integer>`; `query` is null for metadata/overview mode or a non-blank
literal search string of at most 256 characters. Blank or whitespace-only
queries are rejected rather than treated as an overview. The adapter maps
`recent, null` to `reader.inspect("recent")`; every other null query to
`reader.inspect("overview", selector)`; and a non-null query to
`reader.inspect("search", selector, query)`. `recent` with a query is rejected.
Run selectors are `current`, `previous`, `previous_day`, and an exact
case-insensitive `R<positive integer>`. `current` uses only the explicitly
injected ID, never the newest directory. `previous` is the highest safe direct
ID below that explicit current ID (gaps are allowed); malformed evidence in that
run is reported rather than skipped. `recent` returns metadata for at most five
numeric-newest safe run directories and no log excerpts.

`previous_day` is resolved from the injected runtime timezone and clock as the
previous local calendar date, using `zoneinfo` calendar semantics across DST.
It filters each model-safe log line by that line's timestamp, rather than by a
Run's start date, so records from a Run spanning midnight are included and
complete prior-day lines in the current active log are eligible. Overview
aggregates category counts, run IDs, safe-line count, first 8 and last 16 lines
across at most 20 safe Runs. Search returns at most 20 literal matches with Run
ID and source line number. Both forms report `calendar_date`, `timezone`, and a
truthful `truncated` flag.

An overview returns validated metadata, category counts, and at most the first
8 plus last 16 model-safe operational lines, deduplicated in source order.
Search is case-insensitive literal substring matching, streams one log, returns
at most 20 matches plus truncation state, and retains source line numbers. A
returned line is capped at 800 characters. A separately opened reader may see
complete records persisted so far in the current, still-appending log; an
unterminated final line is treated as in progress and omitted.

The model-facing projection first requires the exact runtime log envelope: a
millisecond ISO-8601 timestamp with `Z` or a numeric offset, one space, and an
uppercase bracketed category at the start of the line. Tracebacks, stack frames,
exception continuations, and all other unstructured lines are ineligible. It
then conservatively withholds an entire eligible line containing
content-bearing fields such as `text=`, `message=`, `purpose=`, `description=`,
`summary=`, `evidence=`, `focus=`, `query=`, `prompt=`, `utterance=`, or
`response=`, plus the voice-specific `heard=` and wake-word-list `words=`,
**before** matching. Thus history is operational evidence, not a
conversation replay or transcript archive. Tool-result and literal query bodies
are not logged; bounded `[HISTORY]` records contain only episode, operation,
selector, status, match count, and bounded reason.

History uses the existing two-acquisition episode budget alongside
`inspect_self`, `observe_scene`, and `recall_memory`; it gains no semantic-effect
authority and duplicate operator requests retain the existing request-local
cache behavior. It is offered to operator cognition whenever the reader is
configured, and to initiative only through the existing running, enabled, and
active-goal gates. Nothing inspects history automatically at boot or while idle.
Evidence is temporary grounding: it adds neither a separate WorkingMemory turn
nor automatic persistent memory. A history result alone is not evidence that
its information was remembered: when asked whether something is remembered,
the response must distinguish remembered knowledge from what the run record
shows. Persistent-memory evidence may independently support a memory claim.
Current Runtime context remains authoritative,
and a current `started` record is only a partial snapshot—not proof of final
health, success, abandonment, or failure.
