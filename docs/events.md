# Events

`BodyOrientationChanged` announces completion of one semantic logical
orientation transition. It carries previous and resulting yaw/pitch plus the
inherited runtime-owned `source`. The backend completes and authoritative
`RuntimeState.body` is replaced before publication. A successful same-pose
request and a failed request emit nothing. This event is a fact, not the
authoritative pose, servo interpolation, PWM output, telemetry, motion-loop
update, or a high-rate pose stream.

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
the same boolean value produce no duplicate transition. Body orientation
remains authoritative state; only its completed, changed semantic transition
produces the narrowly scoped event described above.

Semantic events may drive deterministic [local reflexes](reflexes.md). They
remain facts, not commands: the reflex independently translates a relevant fact
into a request through an application semantic capability.

Initiative-driven orientation uses the runtime-owned source `initiative`.
Attention accepts only sources beginning with `reflex:`, so that resulting event
is observable but cannot recursively trigger another initiative episode.

## Event projections for attention

Attention may project one selected event into an immutable
`SemanticObservation`. This is neither a replacement for the event nor state:
it identifies the transition while fresh `RuntimeState` describes current
reality. The projection is request-local, retains no raw event, and creates no
history. Supported projections are reflex-sourced `BodyOrientationChanged` and,
when explicitly enabled, existing thermal and memory-pressure raised/cleared
platform events. `PresenceChanged` is deliberately not subscribed: presence
centering can publish the more informative reflex body transition, avoiding
competing episodes without timers, priorities, or coalescing.

## Temporal follow-up due

A valid one-shot timer publishes one `TemporalFollowupDue` with bounded purpose
and relative delay. Attention projects it as `temporal_followup_due` regardless
of platform-attention configuration. Stale/cancelled timers publish nothing;
in-flight attention suppresses the event without queue or retry. See
[Bounded temporal follow-up](temporal-followup.md).
