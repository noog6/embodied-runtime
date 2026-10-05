# Conversation continuity

The runtime keeps three intentionally separate kinds of state:

- **WorkingMemory** is volatile, current-process, rich recent context, including
  dialogue, bounded tool outcomes, and observations.
- **Conversation history** is durable, append-only, bounded completed operator
  dialogue with provider-neutral channel provenance, selected automatically by
  the operator-dialogue harness.
- **Persistent memory** is durable semantic knowledge that is deliberately
  admitted. Ordinary dialogue is not automatically semantic memory.

Conversation history contains only a session/run identity, completion time,
interaction channel, operator text, and assistant text. It excludes transport
identifiers, contact data, credentials, media, tool results, observations,
sensor state, and prompts. It is potentially stale quoted authored material,
not current Runtime authority, fresh evidence, a current instruction, semantic
truth, or proof of delivery. The current `InteractionContext` alone controls
current-channel presentation.

## Version 1 selection

At explicit operator dialogue start, the harness selects up to the three newest
prior-session turns from the current channel and two newest prior-session turns
from other channels. The maximum five are rendered oldest to newest in a
distinct `Prior conversation history` section. Operator and assistant text are
each limited to 2,000 characters. Selection makes no provider calls and uses no
semantic search, summarization, embeddings, or model-visible history tools.

Current-session records are excluded because WorkingMemory owns current-process
continuity. Thus same-process dialogue older than the volatile WorkingMemory
window is not reintroduced from the durable store until a later runtime session.
This is an intentional v1 limitation. Job and autonomous cognition do not
receive conversation history.

## Configuration and lifecycle

The absent section and `enabled = false` both disable the feature. Enabling it
requires a path:

```toml
[conversation_history]
enabled = true
database_path = "../data/conversations.sqlite3"
```

The application owns and closes the SQLite store with its normal lifecycle.
Append and selection failures log bounded metadata; an append failure does not
change a successful response or WorkingMemory.
