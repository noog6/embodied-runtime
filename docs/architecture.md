# Architecture

The runtime should keep these concerns distinct:

- **Platform and hardware:** host computer/operating-system observations are
  distinct from robot hardware adapters and their device capabilities.
- **Embodiment:** robot/body-specific configuration, actuators, and physical
  constraints.
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
The [local runtime console](console.md) is a read-only projection of that state,
not an additional state owner or platform observer.
