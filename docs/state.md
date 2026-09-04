# Runtime state

Authoritative state describes what is currently true. `RobotApplication` owns
the current immutable `RuntimeState` snapshot and replaces that snapshot
explicitly when lifecycle or platform observations change. Callers may inspect
the snapshot, but cannot mutate it.

Application-owned `WorkingMemory` is explicitly **not** part of `RuntimeState`.
It retains bounded, potentially stale history from completed cognition asks;
`RuntimeState` remains the authority for what is currently true. Memory is also
not an event/audit history and does not capture reflex, presence, platform,
camera, or console body activity.

Application-owned `ActiveGoal` is also explicitly **not** part of
`RuntimeState`. It is the single current, volatile intentional commitment, not
a physical fact. `WorkingMemory` remains historical context and may remember
old goal discussions without owning the active goal. Neither abstraction
changes physical-state ownership.

An autonomous `OperatorMessage` is likewise not `RuntimeState`, WorkingMemory,
or persistent history. It is one transient delivery effect through an
application-supplied interaction sink. Its text is not retained for reply
correlation, and a question creates no pending state.

Events announce discrete facts and transitions; they neither own state nor
serve as a state history. Diagnostics are read-only projections of the latest
authoritative state.

Attention's latest trigger, source, state, optional bounded response, latest
initiative action/status, continuation request/action/status/response, outcome
request state, goal-closure result, and bounded outcome response are
volatile developer diagnostics, not `RuntimeState`, WorkingMemory, ActiveGoal,
persistent memory, provider state, or physical truth. They clear on restart and
continuation and outcome fields reset when a new episode begins. They are never fed into later
cognition. A recorded successful closure remains in
diagnostics even if the provider's post-tool continuation fails.

Platform state describes the host computer and operating system independently
of the robot hardware backend. A Raspberry Pi host can therefore run the
virtual hardware backend. Platform observations are captured at startup, on
explicit refresh, and by a lightweight application-owned monitor. Every
successful sample replaces the authoritative platform snapshot before any related event is published. Ordinary
samples are state updates, not events or a telemetry stream.

The monitor announces only thermal-warning and memory-pressure transitions.
Hysteresis prevents threshold chatter, and missing or invalid telemetry neither
changes the tracked condition nor falsely announces recovery. The default
advisory policy samples every 5 seconds, raises/clears thermal warning at
80/75 degrees Celsius, and raises/clears memory pressure at 10/15 percent
available. These runtime thresholds are advisory, not replacements for kernel,
firmware, or hardware safety protection. Monitoring is owned by the application
and is cancelled before hardware and the event bus shut down.

Normal platform samples remain silent state updates. For operator visibility,
the same polling task periodically logs a heartbeat using the latest successfully
installed authoritative platform observation; it does not take another sample and
does not publish an EventBus event. Heartbeat cadence uses monotonic elapsed time,
defaults to 60 seconds, and may be disabled through monitor policy while sampling
and advisory transitions continue normally. Runtime log timestamps instead use
the host's local ISO-8601 wall-clock time and explicit timezone offset. Direct
diagnostic report lines remain structured snapshot output rather than runtime logs.
`RobotApplication` owns one immutable `RuntimeState` snapshot. In addition to
the lifecycle and latest platform snapshot, it owns:

- `BodyState(yaw_degrees, pitch_degrees)`, which is `None` before a body starts
  and is replaced only after a body capability succeeds; and
- `PresenceState(present, source)`, which is `None` until a semantic presence
  observation is received.

Backends, the console, and events do not independently own these facts.
Local reflexes do not add or own authoritative state; they consume semantic
events and request application capabilities that update state through its
existing owner.

Phase 10 continuation data is request-local, not authoritative state. One
immutable WorkingMemory snapshot and exact ActiveGoal identity are captured at
the attention episode's start and reused for initiative, the optional
continuation, and optional outcome evaluation. RuntimeState is freshly projected
for each request. `InitiativeContinuationStimulus` contains only the first effect
name/status/runtime result and original attention identity; outcome grounding
contains one or two immutable effect results. Neither stimulus, autonomous
response, effect result, nor operator message is persisted or appended to
WorkingMemory. Continuation diagnostics are latest-episode volatile metadata
only.

## Observations are not state

`SemanticObservation` is an immutable, request-local projection of one semantic
transition used only to explain an attention wake. The application installs
platform state before the corresponding event is published, so cognition gets
fresh authoritative temperature and memory facts from `RuntimeState`; attention
never resamples the provider. Observations are not retained in state,
WorkingMemory, event history, or a queue.

## Self-inspection results

Self-inspection results are immutable request-local grounding, not `RuntimeState`,
working memory, event history, or persistent state. See
[Bounded semantic self-inspection](self-inspection.md).
