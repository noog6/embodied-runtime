# Findings and deliberate search

A **Finding** is one immutable, bounded claim published during an exact JobRun.
It records the source Job, Run, Task, attention episode, publication time, topic,
kind, claim, and a compact runtime-generated evidence basis. Its stable operator
identity is `FIND<n>`.

Findings are historical Job-authored research notes. They are **not** current
RuntimeState or sensor truth, verified facts, Job Workspace material, JobRun result
reports, WorkingMemory, or persistent memory. Every cognition-facing result says
`content_authority=job_authored_non_authoritative`; current authoritative evidence
outranks a Finding whenever current state matters.

## Publication and visibility

`publish_finding` is available only to bounded Job work and consumes a normal
semantic-effect slot. The model supplies only `topic`, `kind`, and `claim`. The
runtime derives all identity, time, and evidence provenance from the exact
task-local Job execution context and successful earlier acquisitions in that same
episode. It stores no raw acquisition payload and performs no memory admission,
conversation mutation, notification, Workspace write, or result-report update.

An `observation` requires fresh runtime-inspection or sensor-observation evidence.
A `synthesis` may instead rely on historical Findings, the current Job's Workspace,
persistent memory, run history, or other successful acquisitions. Repetition does
not increase authority: a synthesis based on `search_findings` remains marked
`historical_finding`.

Publication stages the record while its source run is running. Ordinary search
exposes it only after that exact run completes successfully. Findings from running,
failed, stopped, interrupted, or pending runs remain durable audit history and can
be inspected administratively, but are not reusable search results.

## Search

`search_findings` is a deliberate read-only acquisition available in explicit
operator cognition and bounded Job cognition. It searches topic, claim, and source
Job name using deterministic case-insensitive lexical matching, with a default
limit of five and hard maximum of ten. Results are relevance ordered and newest
first within equal relevance. Each result includes bounded provenance, evidence
basis, source-run completion metadata, and an explicit authority warning.
The `limit` field is optional; omitting it selects the default of five. Multi-token
queries use high-recall partial matching: more matching tokens rank above fewer,
exact topic matches receive extra weight, and equal scores are newest-first.
The SQLite corpus is streamed one row at a time and only the best requested result
window is retained in Python, so both output and Python ranking memory are bounded
by the limit. Phase-1 lexical scan time may still grow with the completed-Finding
corpus; indexed full-text or semantic search remains deferred.

A Job can discover another Job's completed Findings or its own prior-run Findings,
but receives no access to the source Job's Workspace. Search consumes the ordinary
episode acquisition budget and does not modify WorkingMemory or persistent memory.

The console commands `findings`, `findings <query>`, and `finding show FIND<n>`
provide bounded administrative inspection without cognition or mutation.

Deliberate publication and search emit bounded `[FINDINGS]` operational logs with
episode and source identity where available, argument lengths, match counts, and
status. Claim text and full search queries are never logged.

Automatic context selection, embeddings, promotion to persistent memory,
retraction/supersession, contradiction handling, expiry, and periodic reflection
are explicitly deferred to later Knowledge Integration phases.
