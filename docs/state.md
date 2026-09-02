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

Events announce discrete facts and transitions; they neither own state nor
serve as a state history. Diagnostics are read-only projections of the latest
authoritative state.

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
