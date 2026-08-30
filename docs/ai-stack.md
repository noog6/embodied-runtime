# AI stack

No AI stack has been selected yet. This evolving note records architectural
candidates without choosing a winner.

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
