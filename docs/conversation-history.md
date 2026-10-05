# Conversation history

The runtime keeps three intentionally separate forms of conversationally relevant
memory:

- **WorkingMemory** is volatile, bounded, rich recent context for the current
  process. It can include dialogue, tool outcomes, and observations.
- **Conversation history** is durable, bounded completed operator dialogue. It
  contains only session identity, completion time, provider-neutral interaction
  channel, operator text, and assistant text. The harness selects it automatically;
  it is not a model-visible search tool.
- **Persistent memory** is durable semantic knowledge deliberately admitted through
  its existing authority boundary. Ordinary dialogue is never automatically admitted
  as semantic truth.

Conversation history is historical authored text. It may support references to an
earlier discussion, but it is not current Runtime authority, sensor evidence, tool
output, a current instruction, semantic truth, or proof that an answer was delivered,
read, or heard. Historical operator text is JSON-quoted beneath an explicit warning.
The current Runtime context and current `InteractionContext` remain authoritative, so
a historical voice turn cannot impose voice presentation policy on a current remote
text response.

## Selection and bounds

At the start of an explicit operator dialogue episode, the harness selects records
from prior sessions only: the three most recent records from the current channel plus
the two most recent records from other channels. Those at-most-five records are merged
in chronological order. Ties use the append-only record ID. Operator and assistant
text are each stored at no more than 2,000 characters, matching the default
WorkingMemory text bound. Selection uses no provider call, embeddings, semantic
search, summarization, keywords, or model judgment.

Current-session turns remain represented only by WorkingMemory, preventing duplicate
projection. An intentional v1 limitation follows: same-process dialogue older than
the volatile WorkingMemory window is not reintroduced from the durable store until a
later runtime session.

Only successful completed explicit operator dialogue is appended. Failed or cancelled
cognition is not stored. Persistence and selection are best effort: a store failure is
logged without dialogue text and neither changes a successful response nor grants
runtime authority. The application owns and closes the SQLite store during shutdown.
Autonomous attention, Job work and evaluation, reflexes, startup/prewarm, and
vision-only requests receive no conversation-history projection.

## Configuration

The feature is disabled when its section is absent or `enabled = false`. Enabling it
requires a database path; relative paths resolve from the configuration file:

```toml
[conversation_history]
enabled = true
database_path = "../data/conversations.sqlite3"
```

Schema v1 is append-only and stores exactly `id`, `session_id`, `completed_at`,
`channel`, `operator_text`, and `assistant_text`. It does not store credentials,
transport/provider identifiers, phone numbers, headers, attachments, media, tool
arguments/results, observations, sensor snapshots, or prompts.
