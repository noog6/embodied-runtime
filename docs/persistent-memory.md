# Persistent memory

Working memory is volatile recent conversational context. Persistent memory is
durable, explicitly accepted knowledge. Cognition can access the latter only
through bounded capabilities: `recall_memory` is a deliberate read-only
acquisition, while `remember` is an operator-grounded durable semantic effect.

## Operator-grounded admission

Admission is selective and opt-in during an ordinary operator episode. The
model may propose at most one durable fact, preference, or simple relationship;
the runtime admits it only when a short evidence span is present in the current
operator utterance and the structured value occurs in that evidence. Comparison
uses NFKC normalization, whitespace collapsing, and case folding, and rejects
control characters. Recalled memory, working memory, assistant conclusions,
sensor interpretation, and startup instructions are not admission evidence.

Subjects must already exist and resolve to exactly one entity by canonical name
or alias, and the submitted subject query must occur in the evidence clause. A
relationship may add one related entity, also resolved exactly; its query must
occur in evidence and equal the structured value. Cognition-created predicates
and related roles are NFKC-normalized, token-validated, and case-folded at the
admission boundary so capitalization cannot create distinct machine keys.
Conversational cognition cannot create entities or aliases: learning a fact
about a known thing is deliberately separate from deciding that a new durable
thing exists. Entity administration remains a supervised console operation.
There is no fuzzy, semantic, typo-correcting, lexical, embedding, or vector
fallback.

Every accepted write receives a runtime-owned `operator_statement` source,
active status, normal store creation time, and exactly one inline `text/plain`
payload. Both the durable summary and payload are the normalized display form
of the verbatim evidence clause, never model-authored prose. The subject link
always has role `subject`; cognition may name
only the optional related link role. Identical kind/predicate/value/direct-link
sets are successful no-op duplicates. A fact or preference with the same
predicate but a different value is conservatively rejected. Admission never
updates, deletes, supersedes, adjusts confidence, or reconciles existing memory.
The durable evidence text and entity/value anchors are operator-grounded.
Predicate and related-role fields are model-proposed structured metadata over
that evidence; the evidence remains the authoritative durable source.

`remember` is not available to autonomous attention, initiatives, goals,
temporal follow-ups, or background monitors. Nothing mines transcripts or runs
post-turn extraction: without an explicit `remember` call no durable write is
attempted. Future work may separately define supervised conversational entity
admission and supersession semantics.

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

## Bounded cognition recall and admission

Phase 18.3 exposes configured persistent memory through one read-only cognition
tool, `recall_memory(query: string)`. Recall is a deliberate Phase 16
acquisition, so a success, miss, ambiguity, validation failure, or store failure
uses one of the episode's existing two acquisition attempts. It continues the
same episode with freshly reconstructed runtime and temporal grounding. No
memory inventory or search result is automatically injected, and operator text
is never pre-searched.

The query is one non-empty canonical name or alias of at most 256 characters.
Lookup uses only the store's exact normalized identity matching. At most five
matching entities and the eight newest active memory IDs per entity are
projected. That newest subset is rendered in ascending ID order; entity and
output truncation are explicit, and rendered text is capped at 12,000
characters. Misses are explicit. Ambiguous exact matches remain in entity-ID
order for cognition to address rather than being selected by the runtime.
Directly linked entity IDs and names are resolved, but recall performs no graph
traversal and does not load payload bodies.

Persistent recall is historical evidence and may be stale relative to present
physical reality. Cognition should describe uncorroborated recalled state as
remembered knowledge; current sensor claims require fresh perception.

```text
WorkingMemory
    volatile recent context

PersistentMemory
    durable accepted knowledge

recall_memory
    bounded deliberate read acquisition

remember
    bounded operator-grounded durable write effect
```

Explicit console commands remain the administrative admission path. Operator
cognition may additionally request one bounded `remember` effect under the
admission rules above. Recall and admission cannot create entities, and neither
capability can update or delete existing memories. There is no automatic or
autonomous admission. This phase adds no fuzzy, full-text, semantic, embedding,
or vector behavior; ranking by a model; object-file retrieval; compaction;
archival; supersession; contradiction reconciliation; background learning; or
new attention trigger or budget. Schema version 1 and its lifecycle remain
unchanged.
