# AI stack

Active, bounded image interpretation is documented in
[`visual-perception.md`](visual-perception.md). It is a separate provider-neutral
perception adapter rather than a camera or authoritative state concern.

The implemented AI experiment uses the OpenAI Responses API for grounded text,
bounded runtime-owned working memory, and one semantic capability request:

```text
operator text + request-time runtime projection -> text cognition
    -> semantic capability request -> runtime validation/execution
    -> refreshed authoritative state -> final cognition response
```

Across asks, the application explicitly composes current runtime facts, one
current active goal, the current operator request, and up to six completed turns:

```text
current runtime facts + current active goal + bounded working memory
                                      + current operator request -> cognition
```

Provider requests remain independent. This volatile FIFO supplies continuity
without provider conversations. Historical context may be stale, so the fresh
authoritative runtime projection always wins for current robot facts. There is
no persistent memory, retrieval, embedding, or summarization layer.

Phase 5 keeps the owners distinct: `RuntimeState` owns current reality,
`ActiveGoal` owns the single current intention, and `WorkingMemory` owns bounded
history. The provider owns none of them. Goal text cannot override current
operator input, runtime facts, safety, or capability validation. Setting a goal
does not perform it, and no goal wakes cognition, retries, monitors satisfaction,
or resolves itself.

It exists to test the responsibility boundary between the runtime and cognition;
it does not select the reference robot's permanent AI architecture. The
application creates an allow-listed immutable projection of its current state
immediately before every request. Authoritative state remains runtime-owned and
is neither handed to nor queried by the cognition backend. Each request remains
independent. AI intent is advisory: the runtime decides whether and how an
action occurs. Autonomous semantic capabilities are explicitly projected:
`orient_body` remains limited to a nonphysical body, while `address_operator`
is independent of body physicality. Without continuation, the runtime accepts at most one initiative
capability request per episode. Cognition-driven physical actuation is not
approved. No model-controlled or persistent memory, images, Realtime, or audio
capability is added. See
[OpenAI text cognition setup](cognition-openai.md).

Realtime, chained, and hybrid approaches remain open candidates.

## Goal-directed attention and bounded initiative

Phase 6 is explicitly opt-in and transition-driven:

```text
semantic runtime event + ActiveGoal
                 -> deterministic attention gate
                 -> one read-only autonomous cognition request
```

Only a completed semantic orientation change whose runtime-owned source begins
with `reflex:` can wake this path, and only while a goal exists. The request is
given fresh Runtime context, ActiveGoal, and bounded operator WorkingMemory plus
a provider-neutral attention stimulus. Initiative now has independent capability permissions:

- without `--initiative`, cognition occurs only for explicit operator asks;
- `--initiative` is effect-free but may receive the read-only `inspect_self`
  tool; it receives no semantic effect or continuation without separate policy;
- `--initiative-actions` permits `orient_body` only on a nonphysical body;
- `--initiative-messages` permits `address_operator` only with a configured sink;
- enabling both offers both, but without continuation the runtime accepts at most one request total;
- `--initiative-goal-closure` requires at least one effect permission and permits
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
`--initiative-actions`, and `--initiative-messages` (and therefore today's
`--console` message sink). Request A retains its Phase 9 one-call budget. Only
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

Phase 13 adds one provider-neutral, read-only `inspect_self` capability for four
bounded local areas. Autonomous inspection is limited to one per episode and may
precede—but never count as—at most two existing semantic effects. See
[Bounded semantic self-inspection](self-inspection.md).
