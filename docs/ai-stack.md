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
action occurs. Only absolute orientation on a nonphysical body is exposed, with
at most one execution per request; cognition-driven physical actuation is not
approved. No model-controlled or persistent memory, images, Realtime, or audio
capability is added. See
[OpenAI text cognition setup](cognition-openai.md).

Realtime, chained, and hybrid approaches remain open candidates.

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
