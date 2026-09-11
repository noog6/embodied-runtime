# Persistent memory

Working memory is volatile, bounded conversational context for the current
runtime session and may disappear on restart. Persistent memory is durable,
searchable knowledge with stable identities that survives process and host
restarts.

> Persistent memory is not a recording of Mira's state stream. It is a durable
> collection of things she has learned or experienced that may be useful again.

## Architecture and runtime ownership

SQLite is the canonical memory store. Its versioned, normalized schema keeps
entities, aliases, memories, entity-memory links, and payloads separate. Integer
database primary keys render as `ENT<n>` and `MEM<n>` and remain stable across
restarts. A memory can link to any number of entities under simple roles, so
facts, relationships, observations, events, and future collection memories use
the same entity-centric foundation.

Canonical names and aliases resolve only by exact normalized keys. Matching
uses Unicode NFKC normalization, Unicode case folding, trimmed whitespace, and
collapsed internal whitespace. Ambiguity returns every exact match in stable ID
order; there is no fuzzy or model-assisted identity matching.

Memories retain readable summaries, optional structured predicate/value data,
simple `source_kind` and `source_label` provenance, optional confidence, and an
`active` status. Historical timestamps are stored as deterministic ISO-8601 UTC
values. They are deliberately separate from session-local monotonic timing.

Payloads are records distinct from memories. Phase 18.1 creates inline UTF-8
text payloads only, while the schema reserves object payload metadata through
an opaque object reference, media type, and SHA-256 digest. It stores no binary
media. A later object-store layer may use a companion layout such as:

```text
data/
  memory.sqlite3
  objects/
    ...
```

The object reference is opaque; it is not necessarily a filesystem path.
Future full-text, embedding, or vector indexes are retrieval aids and are never
canonical memory truth.

## Durability and boundaries

Creating a memory, its links, and its payloads is one SQLite transaction.
Foreign keys are enforced and failures roll the whole operation back. Schema
version 1 uses `PRAGMA user_version`; unsupported or unversioned non-empty
databases fail rather than being recreated. In Phase 18.2 the CLI composition
boundary constructs the configured store and supplies it to the application.
The application owns that store and closes it once, after operator cognition,
attention, temporal, and hardware work has stopped.

## Configuration and operator console

Persistent memory is opt-in. An absent section, or `enabled = false`, does not
create a database or its parent directory. When enabled, a non-empty path is
required:

```toml
[memory]
enabled = true
database_path = "../data/mira-memory.sqlite3"
```

Relative database paths resolve against the configuration file's directory,
not the process working directory; absolute paths remain absolute. The launch
composition layer creates a missing parent directory before SQLite opens the
file. Unsupported or invalid databases fail startup rather than silently
disabling persistence.

The local console provides deterministic administrative commands:

```text
memory persistent
memory entity add <entity_type> "<canonical name>"
memory entity find "<name>"
memory alias add ENT<n> "<alias>"
memory find "<name>"
memory add <kind> "<summary>" [--link ENT<n>:<role>]...
    [--predicate <token>] [--value "<text>"]
    [--source-kind <token>] [--source-label "<text>"]
    [--confidence <0.0..1.0>]
memory show MEM<n>
memory list ENT<n>
```

Manual writes create exactly one inline `text/plain` payload containing the
summary. `memory find` uses exact normalized canonical-name/alias lookup and
shows every match in entity-ID order with its linked active memories. It does
not perform substring, typo, fuzzy, semantic, embedding, or model-assisted
matching. Entities and memories retain the same `ENT<n>` and `MEM<n>` identity
after a full application stop and a new application opens the same file.

The existing `memory` and `memory clear` commands still inspect and clear only
volatile `WorkingMemory`.

Persistent memory is not exposed as a cognition tool, injected into prompts,
or searched from operator utterances. Only explicit console commands admit
durable records; conversations, observations, goals, attention, runtime events,
and telemetry are not admitted automatically. This phase has no automatic
recall or admission, embeddings, semantic similarity, object-file writing, state-stream
recording, compaction, deletion, archival, supersession, contradiction policy,
or collection-specific behavior. SQLite is intended for ordinary serialized
use by one local runtime process; there are no pools, workers, services, or
distributed-concurrency mechanisms.
