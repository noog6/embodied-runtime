# Run history

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
