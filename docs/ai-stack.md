# AI stack

Active, bounded image interpretation is documented in
[`visual-perception.md`](visual-perception.md). It is a separate provider-neutral
perception adapter rather than a camera or authoritative state concern.

Text cognition uses replaceable OpenAI Responses or compatible Chat Completions
adapters. The runtime owns grounding, bounded acquisition and effect budgets,
and validation; providers transport requests and results:

```text
operator text + request-time runtime projection -> text cognition
    -> semantic capability request -> runtime validation/execution
    -> refreshed authoritative state -> final cognition response
```

Across asks, the application explicitly composes current runtime facts, the
applicable active-goal projection, the current operator request, and bounded
historical context, including up to six completed WorkingMemory turns:

```text
current runtime facts + current active goal + bounded working memory
                                      + current operator request -> cognition
```

Provider requests remain independent. This volatile FIFO supplies continuity
without provider conversations. Historical context may be stale, so the fresh
authoritative runtime projection always wins for current robot facts. The runtime
also supports configured durable [conversation history](conversation-history.md), bounded
[persistent-memory recall and admission](persistent-memory.md), and
[Finding context](findings.md). These are distinct from WorkingMemory and are
selected by the runtime, not retained provider sessions. Persistent-memory recall
uses exact entity matching, not embeddings or vector search.

The owners remain distinct: `RuntimeState` owns current reality,
`ActiveGoal` represents an exact runtime-owned intention, and `WorkingMemory` owns bounded
history. The provider owns none of them. Goal text cannot override current
operator input, runtime facts, safety, or capability validation. Setting a goal
does not perform it, and no goal wakes cognition, retries, monitors satisfaction,
or resolves itself.

A Task's optional `TaskGoal` describes its desired outcome and survives that
Task's lifecycle transitions; Task snapshots themselves are session-resident.
A running Task may bind an exact session-local `ActiveGoal`. Each live Job
occurrence owns its own Task/goal binding in a `JobExecutionContext`; foreground
`current_task` and `active_goal` views are not global execution authority.
Pausing releases that Task's resources and active intention; resuming creates
a fresh active intention. Generic goal mutation cannot replace a Task-owned goal.

The runtime implements resource arbitration, durable Jobs and JobRuns,
calendar/event activation, and bounded continuation. It does not persist or
recover live Task execution contexts. Shutdown drops volatile Task ownership
and interrupts nonterminal JobRuns under the existing persistence policy;
it does not assert semantic Task completion. See [Jobs](jobs.md) and
[execution-context ownership](architecture.md#job-execution-context-ownership).

These implementations preserve the responsibility boundary between runtime and
cognition without fixing the reference robot's permanent AI architecture. The
application creates an allow-listed immutable projection of its current state
immediately before every request. Authoritative state remains runtime-owned and
is neither handed to nor queried by the cognition backend. Each request remains
independent. AI intent is advisory: the runtime decides whether and how an
action occurs. Autonomous semantic capabilities are explicitly projected:
`orient_body` remains limited to a nonphysical body, while `address_operator`
is independent of body physicality. Without continuation, the runtime accepts
at most one initiative semantic effect per episode, independently of the two
acquisition attempts. Cognition-driven physical body actuation remains restricted.
Image interpretation, retained-image workflows, and bounded voice interaction
are implemented through separate runtime capabilities; no persistent provider
session or Realtime integration supplies execution continuity. See
[OpenAI text cognition setup](cognition-openai.md),
[compatible cognition](cognition-compatible.md), and
[bounded voice conversation](voice-conversation.md).

The bounded chained voice path is implemented; direct speech-to-speech and
hybrid approaches remain candidates.

## Goal-directed attention and bounded initiative

General initiative is explicitly opt-in and transition-driven:

```text
semantic runtime event + ActiveGoal
                 -> deterministic attention gate
                 -> one bounded autonomous cognition episode
```

A completed semantic orientation change whose runtime-owned source begins
with `reflex:` can wake this path while a goal exists. Opt-in platform transitions
and due temporal follow-ups also enter general attention. Durable Job activation
uses its separate scheduling and trigger rules. The request is
given fresh Runtime context, ActiveGoal, and bounded operator WorkingMemory plus
a provider-neutral attention stimulus. Initiative now has independent capability permissions:

- without `--initiative`, cognition occurs only for explicit operator asks;
- `--initiative` enables bounded goal-directed initiative, may receive up to two
  read-only `inspect_self`, `observe_scene`, `recall_memory`, or
  `inspect_run_history` attempts where available, and
  permits the single bounded `schedule_followup` semantic effect;
- `--initiative-actions` permits `orient_body` only on a nonphysical body;
- `--initiative-messages` permits `address_operator` only with a configured sink;
- enabling action and message permissions offers those effects alongside
  scheduling, but without continuation the runtime accepts at most one semantic effect;
- `--initiative-goal-closure` requires only `--initiative` and permits
  one independent post-effect evaluation and same-goal completion after an
  applied effect.

`address_operator` accepts one trimmed, non-empty, control-character-free
plain-text value of at most 1000 characters. Cognition cannot select recipient,
source, transport, or routing. Delivery means the configured sink accepted the
message, not that the human read or acknowledged it. Messages and questions are
volatile effects: they do not enter WorkingMemory, RuntimeState, EventBus history,
or a transcript, and establish no pending answer or reply correlation. Attention
continues to own only selection, one-in-flight lifecycle, cancellation, and
latest diagnostics. There is no polling, retry, unbounded effect sequence, or physical autonomy.

## Direct speech-to-speech

Examples include OpenAI Realtime-style models and future comparable systems.

Potential strengths:

- low latency;
- natural speech interaction;
- interruption and barge-in;
- audio-native behaviour.

Open questions include runtime/model responsibility boundaries, state
ownership, observability, and portability.

## Chained voice pipeline

Speech-to-text -> reasoning/agent -> text-to-speech.

The implemented [voice path](voice-conversation.md) uses this arrangement in a
bounded, half-duplex session. Barge-in and continuous full-duplex conversation
remain outside that implementation.

Potential strengths:

- explicit intermediate state;
- interchangeable reasoning components;
- durable transcripts;
- deterministic orchestration.

Open questions include latency, natural conversational timing, and
interruption handling.

## Hybrid or delegated interaction

A low-latency interaction layer could work alongside separate asynchronous
reasoning, tool, memory, or planning components. This is architecturally
interesting but experimental. Designs must not assume unreleased or unavailable
APIs.

Architecture experiments should compare responsibility boundaries and
behaviour, not merely benchmark model names.

## Phase 10: bounded sequential initiative

`--initiative-continuation` is an explicit opt-in requiring `--initiative`,
plus at least one of `--initiative-actions` or `--initiative-messages` (the
latter requires a configured console or SMS delivery route). Temporal scheduling
supplies the base semantic effect. Request A retains its one-call budget. Only
when its requested effect is `applied`, the application is still running, the
exact episode-start `ActiveGoal` object remains current, and a different freshly
projected capability remains does the application issue one independent Request
B. Request B excludes the first tool and has its own one-call budget, giving a
hard ceiling of two distinct semantic effect requests. A rejection, missing
first effect, stale goal, stopped runtime, or missing remaining capability ends
the sequence without retry or fallback.

Both requests use the one WorkingMemory snapshot captured at episode start;
autonomous text and effects are never appended. Request B receives fresh
RuntimeState plus the original attention stimulus and an immutable
`InitiativeContinuationStimulus` describing the first runtime-produced result.
If goal closure is enabled, one independent outcome evaluation follows the
finished sequence and receives one or two immutable effect outcomes. Completion
is available only when every requested effect applied and same-goal/running
checks still pass. Maintenance goals remain active unless the evaluator actually
requests completion. This is one continuation, not planning, pursuit, retry,
provider state, or a conversation session.

## Phase 11: general semantic observations

Each accepted transition becomes a provider-neutral
`SemanticObservation(kind, source, facts)`. Ordered immutable facts contain
previous/resulting orientation for `body_orientation_changed`, or condition and
transition for `thermal_warning_raised`, `thermal_warning_cleared`,
`memory_pressure_raised`, and `memory_pressure_cleared`. Platform observations
are enabled with `--initiative-platform-attention`, which requires
`--initiative`; effect flags remain independent. Every source remains backend-,
running-, and active-goal-gated, and one in-flight episode suppresses other
sources without queue or replay. Observations and autonomous results are
request-local and never enter WorkingMemory. There is no cognition polling,
observation history, new semantic capability, or direct `PresenceChanged`
attention. Phase 10 works identically for every supported observation kind.

## Bounded semantic self-inspection

The provider-neutral, read-only `inspect_self` capability exposes five bounded
areas: `network`, `storage`, `camera`, `runtime`, and `earcons`. Autonomous self-inspection may consume either or both of the
episode's two read-only acquisition attempts and may precede—but never count as—
the existing bounded semantic effects. See
[Bounded semantic self-inspection](self-inspection.md).

## Phase 15: bounded temporal follow-up

`schedule_followup` is one semantic effect under the existing two-effect ceiling.
One volatile monotonic timer can produce a fresh, same-goal attention episode;
there is no recurrence, persistence, provider session, or attention queue. The
due request takes fresh runtime context and a fresh WorkingMemory snapshot. See
[Bounded temporal follow-up](temporal-followup.md).

## Current episode bounds

The normative current episode grammar and fixed budgets are summarized in
[architecture](architecture.md#current-bounded-autonomy-episode-grammar). In
particular, the explicit grammar permits at most two request-local acquisition
attempts before any effect. Rejections consume slots, the same tool can gather
different information twice, each pass reconstructs Runtime state, and no third
acquisition or acquisition after an effect exists. Acquisition follow-up is not
effect continuation, temporal due work starts a fresh single-flight episode, and
standalone goal closure remains post-effect only. Job outcome evaluation has its
own evidence and progress rules in [Jobs](jobs.md). Concurrency changes admission,
not per-episode budgets: operator requests serialize independently, general
initiative is single-flight, and Job cognition is bounded by
`jobs.max_concurrent_work` (default `1`).

## Finite operator cognition grammar

An operator episode snapshots WorkingMemory once. Its initial independently
grounded request may answer, invoke one operator action and answer, or attempt one
read-only acquisition. Acquisition one permits one fresh request with the same
snapshot and accumulated typed evidence. Acquisition two permits one final fresh
request with acquisitions removed. Thus there are at most three cognition stages,
two acquisition attempts (rejections count), and one non-acquisition capability.
There is no tool loop, recursive continuation, autonomous outcome evaluation, or
provider session. Only terminal prose is returned and persisted.

`play_earcon(cue)` is an ordinary semantic effect in this grammar when earcon output
is enabled and configured. Its strict cue enum and descriptions are derived from the
runtime earcon catalog. The returned result concerns that invocation only: confirmed
runtime playback does not prove a person heard it and does not enact the lifecycle
meaning of the selected cue. Autonomous and Job cognition receive the same effect only
through the existing action permission, fresh-goal/Job authority, and distinct-effect
continuation gates.
