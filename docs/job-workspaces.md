# Job Workspaces

Phase 7B implements one durable Workspace for each durable Job. Every occurrence
of a Job uses the same Workspace; a Workspace is not owned by a JobRun. An
artifact is a regular, bounded UTF-8 text file identified by its canonical
Job-relative logical path. Artifact identity does not involve database rows or
artifact IDs.

When Jobs persistence uses `data/jobs.sqlite3`, the runtime derives the hidden
physical root `data/jobs-workspaces/`, using the database stem plus
`-workspaces`. `JOB7/` beneath that root belongs to Job 7. The shared root is
created during application composition, but Job directories and artifact parent
directories are created lazily by writes. Reads and listings create nothing.
There is no independent Workspace configuration flag or root override.

## Containment and limits

Logical paths use `/`. File paths must be non-empty and directory listings alone
may name the root with an empty string. Absolute, drive-qualified, UNC,
backslash-containing, repeated/trailing-separator, `.`/`..`, NUL/control,
overlong, and over-deep paths are rejected rather than rewritten. Unicode
spelling is preserved. The Linux implementation walks verified directory file
descriptors with no-follow operations. Symlinks at every level, special files,
and multiply-linked artifact files fail closed. Physical paths are never part of
the public result.

The fixed safety ceilings are:

| Limit | Value |
| --- | ---: |
| Logical path | 240 UTF-8 bytes |
| Path component | 100 UTF-8 bytes |
| Path depth | 8 components |
| Returned read | 8,000 Unicode characters |
| One write request | 16 KiB encoded UTF-8 |
| One artifact | 256 KiB |
| One listing page | 100 entries |
| Workspace regular files | 128 |
| Workspace artifact bytes | 8 MiB |

Listings are deterministic, one-level, and cursor-paginated. Reads use Unicode
character offsets, strictly decode the entire bounded artifact, and return the
total and next offsets. Stored and returned `content_version` is the lowercase
SHA-256 digest of the exact bytes.

Malformed paths raise `WorkspaceValidationError`. Valid requests that exceed a
write-request, artifact, file-count, or total-content ceiling instead raise the
distinct bounded `WorkspaceQuotaError`.

## Mutation and durability

The substrate supports only `create`, `replace`, and copy-on-write `append`.
There is no delete, move, rename, search, glob, or recursive listing. Text is
strictly UTF-8 encoded and embedded NUL and unencodable surrogates are rejected.
CRLF, lone CR, whitespace, and trailing newlines are preserved exactly. File
extensions carry no semantics.

Writes stage complete bytes in an unpredictable, exclusive, exact-grammar
reserved temporary sibling, fsync the staged file, atomically publish, and
fsync the containing directory. Create uses Linux `renameat2(RENAME_NOREPLACE)`
so no-clobber publication and removal of the temporary name are one atomic
operation; replacement uses atomic rename. Quotas are checked
through the same contained traversal before publication. A failure before
publication leaves the destination unchanged. If directory fsync fails after
publication, `WorkspaceDurabilityError` truthfully reports `published = true`
and `durability_confirmed = false`; it does not claim rollback.

Ordinary failed creates best-effort remove only the exact empty parent and Job
directories created by that attempt, in reverse order. They never recursively
delete or remove pre-existing directories. Abrupt process death may retain an
empty structural directory, but artifact publication remains old-or-new.

No `expected_version` is required yet, and there is no revision history or
durable provenance ledger. In particular, Phase 7B cannot durably answer which
JobRun last modified an artifact once operational logs are unavailable.

## Authority and meaning

The console offers read-only `job files JOB<n> [directory]` and
`job file JOB<n> <logical-path> [offset_chars]` inspection. It uses the same
bounded store and exact existing Job check. Phase 7B exposes no model-facing
Workspace tools and grants no cognition filesystem authority.

Workspace text is authored working material, not runtime or sensor evidence,
BRD evidence, persistent memory, Job progress, semantic continuity,
configuration, or a JobRun result. Durability certifies neither truth nor
freshness. A note saying that a current temperature is 21 C proves only what the
note contains; a current-world claim still needs fresh supported evidence.
`JobRun.result_report` remains separate immutable terminal historical work
product and is not replaced by an artifact.

Workspace content survives JobRun terminal states, Job disable/enable,
application shutdown, and application restart. Reopening it does not restore or
create a Task, ActiveGoal, WorkingMemory, readiness state, semantic continuity,
progress, or JobRun.
