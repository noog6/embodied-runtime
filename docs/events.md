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
