# AI stack

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
is independent of body physicality. The runtime accepts at most one initiative
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
- `--initiative` is read-only with exactly no tools or continuation;
- `--initiative-actions` permits `orient_body` only on a nonphysical body;
- `--initiative-messages` permits `address_operator` only with a configured sink;
- enabling both offers both, but the runtime accepts at most one request total;
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
latest diagnostics. There is no polling, retry, second effect, or physical
autonomy.

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
