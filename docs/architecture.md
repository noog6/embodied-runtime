# Architecture

The runtime should keep these concerns distinct:

- **Platform and hardware:** operating-system services, hardware adapters, and
  device capabilities.
- **Embodiment:** robot/body-specific configuration, actuators, and physical
  constraints.
- **Sensing and perception:** sensor inputs and their interpretation.
- **Behaviour:** actions, coordination, and task-level control.
- **Cognition:** decision-making, memory, and higher-level reasoning.

These boundaries are intended to keep the reusable runtime independent of a
specific robot or vendor backend. The architecture should emerge through
implementation and validated needs; do not over-design it up front.
