# Bounded Runtime Diagnostics

**Bounded Runtime Diagnostics provides evidence, not authority.** It is a small
runtime-owned instrument panel exposed through the existing cognition tool path.
Diagnostic calls are read-only acquisitions, so operator and Job attention share
the existing two-acquisition episode bound; diagnostics receive no special budget.
The library-level `RobotApplication` default disables diagnostics. Normal Mira CLI
composition explicitly enables them; when disabled they are neither projected nor
directly executable.

## Tools and bounds

* `inspect_runtime_health({})` returns current lifecycle/run identity, elapsed time,
  profile and backend identity, the already-captured `PlatformSnapshot`, capability
  availability, and a small counter selection. Missing platform evidence is `null`.
* `inspect_events({component?, severity?, since_seconds_ago?, limit?})` queries only
  the current process's bounded `RunObservability` ring. The maximum limit is 25,
  lookback is at most 3,600 seconds, filters are exact, and results are newest first.
  Serialized JSON is capped at 24,000 characters by dropping the oldest selected
  events first, preserving valid JSON and setting `truncated=true`.
  It has no pagination or access to evicted events and does not persist events.
  Sensitive metadata keys are omitted. Inspection itself does not add an event, so
  a query cannot contaminate its own result.
* `inspect_effective_config({})` projects only effective profile/hardware/timezone,
  initiative switches, Job availability/auto-continuation/heartbeat/step and poll
  limits, voice availability/wake words/TTS mode, selected camera/cognition/body
  backends, and memory availability. It never dumps TOML, paths, environment
  variables, keys, credentials, passwords, provider secrets, or startup prompts.
* `inspect_job_runtime({})` returns only the current Job occurrence. An idle runtime
  returns `status=idle` and `job_state=no_active_job`. Active output keeps Job,
  JobRun, Task, TaskGoal, and distinct ActiveGoal identity;
  continuation state/readiness/budget/delay/event; evidence-backed counters;
  non-authoritative bounded semantic continuity; and wake evidence separate.

All results are compact JSON with `status`, evidence source, observation timestamp,
bounded scope/completeness, and the current run ID. A small `unknowns` map records
known absence reasons such as `not_reported`, `unavailable`, or `unknown` without
wrapping every value. Invalid fields or bounds receive a structured
`status=rejected` result rather than expanding authority.

## Authority boundary

`inspect_self` remains the coarse semantic inspection surface for network, storage,
camera, and runtime facts. Diagnostics provide narrower structured runtime evidence
and reuse current snapshots rather than initiating broad probes. They cannot execute
commands, read arbitrary files or logs, access the network, capture new media, mutate
configuration or Jobs, control the body, manage processes/services/packages, or read
secrets. Future Job workspaces/files and any future process execution are separate,
separately authorized capabilities; neither is part of diagnostics.
