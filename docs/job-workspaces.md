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
bounded store and exact existing Job check.

During explicit operator dialogue, cognition may deliberately use two read-only
acquisitions: `workspace_list(job, directory="", cursor=null)` lists one
non-recursive directory, and `workspace_read(job, path, offset_chars=0)` reads
at most 8,000 Unicode characters. The Job selector is exact `JOB<n>` or an exact
Job name after surrounding whitespace is trimmed. Name comparison is
case-sensitive Unicode code-point equality, never substring, fuzzy, semantic,
vector, or temporal matching; duplicate exact names return bounded ambiguity.

Both tools share the ordinary maximum of two operator acquisitions and existing
identical-call reuse. They are acquisitions, never effects. They are available
only when both Jobs and Workspace persistence exist and are absent from general
autonomous initiative, notification attention, and non-Job temporal follow-up.

The same explicit operator-dialogue projection offers one effect,
`workspace_write(job, path, mode, content)`, with strict `create`, `replace`, and
`append` modes and an 8,000-character model-facing ceiling. It is available only
when Jobs and Workspace persistence both exist and is never an acquisition. The
current request must explicitly request or
clearly authorize the durable write; cognition must not take notes proactively,
invent a destination, or create a Job. The finite operator grammar permits one
such effect per episode, including after ordinary acquisitions, with no edit loop.

Bounded Job work has a separate authority path. Its provider-facing
`workspace_list(directory, cursor)`, `workspace_read(path, offset_chars)`, and
`workspace_write(path, mode, content)` schemas contain no Job selector. The
harness derives the owner only from the exact current running
Job/JobRun/Task/ActiveGoal binding and revalidates that binding before every
operation. Extra owner arguments fail strict validation, so one Job-work episode
cannot name another Job's Workspace.

Job-work list/read calls consume the same two acquisition attempts as all other
acquisitions. One `workspace_write` consumes an existing semantic-effect
opportunity and participates in the existing continuation ceiling; it adds no
edit loop or Workspace-specific budget. Manual, scheduled, and automatic
continuation work all use this same bounded episode path and exact binding.

Create rejects an existing artifact; replace and append report a missing
artifact rather than creating one. Only an applied, published, durability-
confirmed result supports an unqualified success acknowledgement. Rejection or
unavailability does not prove a change. An indeterminate published result means
the artifact may already have changed but durability was not confirmed, so it
supports neither confirmed durable success nor confirmed failure.

There is no model-facing delete, move, batch write, or `expected_version`, and no
general autonomous Workspace access. A Workspace write neither
admits persistent memory nor creates or modifies a JobRun, Task, progress, or
immutable terminal result.

Workspace text is authored working material, not runtime or sensor evidence,
BRD evidence, persistent memory, Job progress, semantic continuity,
configuration, or a JobRun result. Durability certifies neither truth nor
freshness. A note saying that a current temperature is 21 C proves only what the
note contains; a current-world claim still needs fresh supported evidence.
`JobRun.result_report` remains separate immutable terminal historical work
product and is not replaced by an artifact.

Structured acquisition results distinguish these authority classes: Job
identity, logical paths, entry kinds, byte sizes, content versions, modification
times, pagination/read offsets, and retrieval time are runtime-observed storage
metadata. Artifact text is `authored_working_material` with
`content_authority=non_authoritative`; cognition should attribute it as what the
Workspace artifact says rather than as a fresh observation.

During Job outcome evaluation, list/read results are rendered separately as
non-authoritative Workspace context and their acquisition ordinals are excluded
from committed Job-progress bases. Other authoritative acquisitions keep their
original ordinals. An applied Workspace write remains effect evidence that the
artifact mutation occurred, but not evidence that statements in it are true.
Workspace material is never preloaded as semantic continuity.

Workspace content survives JobRun terminal states, Job disable/enable,
application shutdown, and application restart. Reopening it does not restore or
create a Task, ActiveGoal, WorkingMemory, readiness state, semantic continuity,
progress, or JobRun.
