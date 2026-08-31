# Architecture

The runtime should keep these concerns distinct:

- **Platform and hardware:** host computer/operating-system observations are
  distinct from robot hardware adapters and their device capabilities.
- **Embodiment:** a `BodyBackend` accepts semantic body capabilities. It is
  separate from the low-level `HardwareBackend`; a future physical body may
  use a hardware backend underneath it.
- **Sensing and perception:** sensor inputs and their interpretation.
- **Behaviour:** actions, coordination, and task-level control.
- **Cognition:** decision-making, memory, and higher-level reasoning.

These boundaries are intended to keep the reusable runtime independent of a
specific robot or vendor backend. Interaction and cognition implementations
may interpret runtime state and request semantic capabilities, but they do not
own authoritative physical robot state or hardware safety. The architecture
should emerge through implementation and validated needs; do not over-design
it up front.

Discrete in-process facts may be announced through the transient typed event
mechanism described in [Events](events.md). Events complement, but do not own,
authoritative runtime state and are not capability requests or data streams.
The ownership and snapshot semantics are documented in [Runtime state](state.md).
The [local runtime console](console.md) is a projection of that state and a thin
adapter for explicit development input, not an additional state owner or observer.

Small deterministic [local reflexes](reflexes.md) may consume semantic events
and request semantic application capabilities. The implemented path is sensing
or semantic observation -> authoritative state and event -> reflex -> semantic
capability -> body. A reflex neither owns state nor accesses a body backend
directly, and configured reflex lifecycles belong to the application.
