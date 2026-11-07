# Run history

Each authoritative run directory also receives `summary.json` during process
finalization. This schema-versioned document contains run identity/timing/status,
aggregate operational counters, bounded dimensions, a token `provider_usage` array,
and a hosted `tts_usage` array. TTS entries include a bounded `usage_basis`
(`provider_reported` or `text_length_estimate`) alongside their synthesis,
character, and duration totals. Each cognition provider/model item contains
`requests`, `input_tokens`, `cached_input_tokens`,
`cache_write_tokens`, `output_tokens`, `total_tokens`, and `duration_ms`, in addition to
the bounded provider and model identifiers. These are raw provider-reported usage totals.
The document also includes an explicitly derived cost result. It contains no prompts,
transcripts, credentials, media, or model responses. Summary writing is best effort and
cannot prevent
ordinary shutdown; `run.json` remains the authority for existing run-history status.

Summary schema version 4 adds bounded `provider_calls` metadata with backend,
protocol, endpoint origin, requested/returned model, stage, duration, status,
failure category, usage availability, and normalized raw tokens. `provider_usage`
now includes `usage_available`; unreported token totals are `null` in that bucket
and aggregate metrics, rather than zero. Numeric totals retain their existing
meaning when all attempted requests reported usage. Per-call records preserve
partial usage separately from estimated cost. Compatible endpoint identities
omit paths, userinfo, query strings, and fragments. No provider bodies, prompts,
reasoning blocks, or tool results enter these diagnostic records.
For compatible calls, missing optional cache-token fields also remain `null`; counters do not fill
them with assumed zeros. `provider_requests`, `provider_duration_ms`, dimensions,
and per-model request/duration totals include every attempted outbound call once,
including failed or cancelled calls. Reported tokens count even when the response
is truncated, refused, or malformed. Missing usage from any attempt makes aggregate
token totals and estimated cost unavailable; known per-call counts remain visible
in `provider_calls`. Client setup failures before an outbound attempt are not
provider calls.

Summary schema version 5 adds `role` to `provider_calls`, vision attempt order,
fallback/accepted flags, and safe HTTP error status. `vision_usage` and
`vision_*` counters keep vision separate from the existing cognition usage and
counters, even when both roles use the same direct backend/model. Missing usage
affects only that role's token totals; the overall cost still requires applicable,
complete usage across all roles. Complete direct vision usage has a separate
`cost.components.vision`; compatible model names never inherit direct rates.
The normal perception summary also shows vision requests, fallbacks, and tokens.

Summary schema version 6 adds independent monetary totals and reported-cost
metadata. Verified OpenRouter `usage.cost` is preferred over local estimates,
including for models absent from the catalog and responses with incomplete token
details. `cost.total_usd` and `cost.status` cover reported, estimated, mixed, and
unavailable totals; known reported/catalog subtotals and unknown-call counts remain
visible. `estimated_usd` retains its complete catalog-only meaning. Lifetime totals
survive diagnostic eviction and maintain cognition/vision/TTS separation.
See [OpenRouter response accounting](openrouter-cost-accounting.md) for the complete
schema, verified USD denomination, validation bounds, BYOK semantics, compatibility,
and manual validation procedure. The preceding schema notes describe historical
behavior; missing tokens now affect money only when a catalog estimate is needed.

The `interruptions` counter is incremented once only when the top-level process result
is the authoritative interrupted exit code (130). Task cancellation and lower-level
cleanup do not increment it; completed and other failed runs leave it at zero.

Raw token usage is authoritative for token availability. A call without a valid
reported monetary amount needs an applicable rate in an explicitly identified
`PricingCatalog` to contribute a known estimate. An unknown call makes the complete
monetary total `unavailable` (not zero). Rates
independently cover input, cached-input, and output tokens and no network pricing lookup
is performed. Cached reads and cache writes are subtracted from total input before the
ordinary input rate is applied, so input categories are mutually exclusive. Structurally
inconsistent usage, a missing provider/model rate, or cache-write usage without a
cache-write rate makes the estimate unavailable. The OpenAI Responses adapter records
provider-reported cached reads and cache writes when present; absent detail fields are
recorded as zero. Normal provider request log lines likewise include
`cache_write_tokens=<n>` when that valid provider detail is present. Local speech
activity is not assigned hosted cost. ElevenLabs usage is recorded after complete WAV
collection and before playback, so a later local playback failure still retains the
synthesis estimate; a rejected pre-synthesis request does not invent usage. The
official SDK's raw-response synthesis path exposes `character-cost` while retaining
asynchronous audio collection. A valid bounded non-negative integer from that header
is preferred as billable character usage; missing or unsafe metadata falls back to
requested text length without affecting speech success.

The versioned built-in catalog covers OpenAI Responses cognition using
the exact model key `gpt-5.6-luna`, at the standard pricing applicable to requests with
at most 272,000 input tokens. A request above that boundary, or usage from any unknown
provider or model, makes the run estimate unavailable rather than applying an unsafe
fallback. It also covers ElevenLabs `eleven_flash_v2_5` at the public Flash/Turbo API
estimate dated 2026-10-02: USD $0.05 per 1,000 billed characters. Cost components expose
cognition, vision, and TTS separately; `estimated_usd` is populated only for
complete catalog-only totals, and `total_usd` covers all complete totals. This public-rate
estimate is not an account invoice: subscriptions, included credits, taxes, and final
marginal charges may differ. eSpeak has no external API cost. Unknown actually used
hosted TTS models make cost unavailable rather than silently appearing as zero.

This locally calculated value estimates only usage represented in priced provider
accounting; it is not a billing authority or a complete robot operating cost. Built-in
pricing is dated and versioned, performs no runtime network lookup, and may become stale.
The current summary observability schema is version 6. Schema 3 introduced the
bounded TTS usage basis; neither response headers nor arbitrary provider metadata
are persisted.

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
ID and source line number. Both forms report their resolved calendar/UTC window,
log source coverage, omitted detail, and a conservative legacy `truncated` flag.
See [Daily recorded Job activity](#daily-recorded-job-activity) for the
application-composed database accounting and precise coverage semantics.

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
selector and status, operation-appropriate returned counts, bounded coverage
metadata, and bounded reason. Overview telemetry does not report search matches.

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

## Daily recorded Job activity

The application augments only `inspect_run_history(previous_day, null)` with
`job_activity` from the configured Jobs store. The application resolves one
`LocalDayWindow` using its effective timezone and aware wall clock, and passes
that same window to both readers. `calendar_date`, `timezone`, `utc_start`,
`utc_end`, and `retrieved_at` identify the review. The UTC interval is half-open
`[utc_start, utc_end)`; its endpoints are separate local midnights, so Toronto
spring/fall days contain 23/25 hours. Retrieval time identifies the read, not a
historical status snapshot or an atomic snapshot across filesystem and database.

Accounting is over timestamp-consistent retained `job_runs` rows in this one
configured database, across Jobs regardless of current enablement or target.
A qualifying row has a creation, recorded start, or terminal timestamp inside
the interval, or was recorded open immediately before either endpoint. Counts
are exact over that declared scope, independent of detail limits:

- `records`: distinct qualifying rows.
- `created`: admissions/record creations in the interval.
- `started`: persisted transitions to `running` in the interval. These precede
  bounded cognition and do not prove an episode or provider call executed.
- `terminal_outcomes`: `completed`, `failed`, `stopped`, and `interrupted` rows
  with `finished_at` in the interval. Episode completion is not counted here.
- `statuses_at_retrieval`: current persisted states of qualifying rows, including
  `pending` and `running`. A later completion is not assigned to the reviewed day.
- `open_before_start` / `open_before_end`: rows created strictly before that
  endpoint with no recorded finish before it. A finish exactly at an endpoint
  remains open immediately before that endpoint. These are record-lifetime facts,
  not claims about execution, uptime, pause/readiness state, or crash time. In
  particular, startup reconciliation timestamps when interruption was recorded.

These categories overlap: do not sum them as disjoint populations. For consistent
rows, `open_before_end = open_before_start + created - sum(terminal_outcomes)`.
Actual execution/readiness at a past midnight is unknown because it is not
persisted as a transition history.

Malformed/naive timestamps, missing required timestamps, lifecycle-inconsistent
fields, reversed chronology, or timestamps later than retrieval are excluded
from these counts. `coverage.unclassified_records` counts such rows across the
entire retained table: their day membership is uncertain, even if an individual
field appears outside the day. Coverage is then `partial`, not a zero-activity
claim. A null start is valid for pending rows and for stopped/interrupted rows
that never transitioned to running. Database unavailability returns
`status=unavailable` with no zero totals.

`totals` and each per-Job `counts` use the above names. `jobs` contains at most
10 numeric-Job-ID-ordered groups. `runs` contains at most 20 operational metadata
records: terminal non-completed outcomes in the day first, then records open
before day end, then the remainder; each tier uses ascending Run ID. Details
include only IDs, validated timestamps, current status and accounting flags.
`omitted_jobs` and `omitted_runs` are exact counts of undisplayed groups/rows;
they do not reduce aggregate completeness. No names, descriptions, summaries,
errors, reports, Workspace material, or schedule data are selected.

Aggregation and bounded detail selection share one SQLite statement/read
snapshot, without a write transaction or schema migration. SQLite may materialize
internal working sets; Python receives only the bounded result. The summary
requires SQLite 3.35+ for materialized CTEs, avoiding repeated timestamp parsing
and aggregation over the retained table. An older Python SQLite binding returns
`sqlite_activity_aggregation_unsupported`; existing log evidence remains usable.
The summary is operational metadata under existing history-inspection authority,
including autonomous Job work with a live exact goal binding. It does not enable
cross-Job authored-content reads or change `shared_reads_enabled`, tool schemas,
episode budgets, persistence, scheduling, or Job descriptions. Interpretation
and notification decisions remain with cognition.

Log coverage is independent. Previous-day results expose `log_coverage` with
discovery status, discovered/examined/omitted directory counts, readable-log count,
and unavailable Run IDs. Only the newest 20 safe direct runtime directories are
examined, before filtering lines by day; omitted directories might contain
relevant evidence. An inaccessible root or no readable selected logs is
`unavailable`, a readable empty root is empty accessible evidence, and a mix of
readable and unavailable/omitted sources is `partial`. A read error after some
eligible records preserves those records as partial evidence.
`unclassified_timestamp_lines` counts otherwise model-safe lines whose malformed
timestamps prevent day selection; it also makes coverage partial. Privacy
filtering precedes that count. Counts describe only accessible complete model-safe
records with valid timestamps read so far. `log_details` separately reports
returned lines, exact accessible omitted lines (matching lines for search), and
clipped lines among those returned. Omitted-line count is unknown when no source
is accessible. Legacy `truncated` is conservative
across log source limitations or log detail omissions; it must not be used as a
database-completeness indicator. Privacy filtering is unchanged. No view can
establish how many artifacts or database rows were deleted before inspection.

An unavailable database preserves log evidence; unavailable logs preserve the
database summary. Neither source establishes continuous uptime, correct/useful
Job outputs, or fulfillment of scheduled obligations. Schedule definitions can
change, admission can legitimately defer/coalesce, and schedule history is not
stored here. Whole-runtime process counters are not daily counters.

### Read-only deployment validation

1. Request a previous-day overview through the normal operator path. Retain its
   resolved window, retrieval time, source coverage and counts. Confirm the
   effective timezone and configured database/history roots; do not substitute
   a checkout's defaults. Check the service interpreter's SQLite version with
   `python3.13 -c 'import sqlite3; print(sqlite3.sqlite_version)'` (3.35+).
   This inspection changes no Job definition or schedule.
2. Use `sqlite3 -readonly /configured/path/jobs.sqlite3` against that same database.
   For example, Toronto November 6, 2026 has these UTC bounds. The direct query
   below assumes the writer's canonical UTC microsecond strings (`...000000Z`).
   Check that assumption and `unclassified_records=0` first; if legacy/noncanonical
   or inconsistent rows exist, independently normalize/validate their aware
   timestamps before comparing, rather than treating text order as time order.

   ```sql
   PRAGMA query_only=ON;
   BEGIN;
   WITH window AS (
     SELECT '2026-11-06T05:00:00.000000Z' AS s,
            '2026-11-07T05:00:00.000000Z' AS e
   )
   SELECT status, COUNT(*) AS records,
          SUM(created_at >= s AND created_at < e) AS created,
          SUM(COALESCE(started_at >= s AND started_at < e, 0)) AS started,
          SUM(COALESCE(finished_at >= s AND finished_at < e, 0)) AS terminal_in_day,
          SUM(created_at < s AND (finished_at IS NULL OR finished_at >= s)) AS open_before_start,
          SUM(created_at < e AND (finished_at IS NULL OR finished_at >= e)) AS open_before_end
   FROM job_runs, window
   WHERE created_at < e AND (finished_at IS NULL OR finished_at >= s)
   GROUP BY status ORDER BY status;
   ROLLBACK;
   ```

   Sum the rows to compare aggregate admissions/starts and record counts; terminal
   rows compare with the corresponding terminal-outcome bucket. The status groups
   compare with retrieval-time status, allowing for updates between the two
   independent snapshots. Inspect a few returned Run IDs directly with metadata-only
   queries. Verify omitted-detail counts separately from aggregate completeness.
3. Check a day with multiple runtime sessions, including a still-active session
   without `summary.json`. Compare the reported accessible directories with the
   retained history inventory. Missing/inaccessible/omitted sources must remain
   explicit; deletion history and process uptime cannot be inferred from inventory.
4. After the operator separately updates the deployed review instructions, inspect
   the next nightly JobRun result and its referenced Workspace report using existing
   result/Workspace reads. Confirm it used the summary, attributed limitations,
   distinguished outcomes from usefulness, retained its report, and completed
   without extra episodes or routine notification. This is deployment acceptance,
   not evidence supplied by the offline tests. No instruction migration or live
   configuration/service/database modification is part of this implementation.
