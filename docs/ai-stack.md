# AI stack

The implemented AI experiment is a grounded, text-only cognition vertical slice
using the OpenAI Responses API:

```text
operator text + request-time runtime projection -> text cognition
```

It exists to test the responsibility boundary between the runtime and cognition;
it does not select the reference robot's permanent AI architecture. The
application creates an allow-listed immutable projection of its current state
immediately before every request. Authoritative state remains runtime-owned and
is neither handed to nor queried by the cognition backend. Each request remains
independent and text-only. No memory, tools, actions, images, Realtime, or audio
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
