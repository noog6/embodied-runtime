# Events

Runtime communication distinguishes three concepts:

- **State** describes what is true now, such as battery voltage, current pose,
  or motion state. Its authoritative owner is runtime state, not an event.
- **Commands or capability calls** request that something happen, such as
  speaking text or performing a gesture. Events are not request/response RPC
  and are not a command bus.
- **Events** are discrete semantic facts that something happened, such as
  presence being detected, motion completing, or an interaction being
  interrupted.

The `EventBus` is typed by event class, local to one process, transient, and
owned by the application. Each subscription has bounded ordered buffering and
an independent async handler worker; a failing handler is logged without
stopping other delivery. Full buffers apply backpressure rather than silently
discarding events.

Events do not own authoritative state and are not persistent history. Camera
frames, PCM audio, IMU samples, servo telemetry, and other high-rate data are
streams and do not belong on this bus. Persistence, replay, event sourcing,
and distributed messaging are not currently intended.

Platform polling updates authoritative state without emitting a sample event.
Only meaningful advisory transitions are announced: `ThermalWarningRaised`,
`ThermalWarningCleared`, `MemoryPressureRaised`, and `MemoryPressureCleared`.
Continuous platform telemetry does not belong on the `EventBus`.

`PresenceChanged(previous_present, present)` announces semantic presence
transitions. The application installs the new authoritative `PresenceState`
before publishing, so handlers see the new truth. Repeated observations with
the same boolean value produce no duplicate transition. Body orientation is
authoritative state and deliberately does not produce an event.

Semantic events may drive deterministic [local reflexes](reflexes.md). They
remain facts, not commands: the reflex independently translates a relevant fact
into a request through an application semantic capability.
