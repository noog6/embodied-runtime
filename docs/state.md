# Runtime state

Authoritative state describes what is currently true. `RobotApplication` owns
the current immutable `RuntimeState` snapshot and replaces that snapshot
explicitly when lifecycle or platform observations change. Callers may inspect
the snapshot, but cannot mutate it.

Events announce discrete facts and transitions; they neither own state nor
serve as a state history. Diagnostics are read-only projections of the latest
authoritative state.

Platform state describes the host computer and operating system independently
of the robot hardware backend. A Raspberry Pi host can therefore run the
virtual hardware backend. Platform observations are captured at startup and on
explicit refresh; this phase does not poll or publish platform-change events.
