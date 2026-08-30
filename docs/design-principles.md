# Design principles

These durable constraints guide the runtime without prescribing a detailed
implementation.

1. **The runtime stands on its own.** Basic boot, hardware state, sensing,
   diagnostics, safe motion, and basic behaviour must work without an AI model
   or cloud service.
2. **AI implementations are replaceable.** No model or API is the backbone of
   embodied-runtime. Realtime models, chained voice pipelines, future
   GPT-Live-style systems, local models, and other services connect through
   deliberate boundaries.
3. **Add seams, not a speculative framework.** Keep interaction and cognition
   boundaries clean, but do not build a broad provider abstraction before
   concrete requirements demand it.
4. **Separate interaction from deeper cognition.** One model may implement
   both, but the architecture must also permit low-latency conversation beside
   asynchronous reasoning, tools, memory, search, or planning.
5. **The runtime owns authoritative robot state.** Pose, battery, sensing,
   motion, attention, hardware capabilities, persistent memory, and similar
   facts have deterministic runtime ownership. AI context receives only a
   representation or projection of that state.
6. **AI requests semantic capabilities.** Models request gestures, behaviours,
   or other meaningful capabilities; deterministic runtime code validates
   safety and constraints, maps them to the embodiment, and executes them.
7. **Safety outranks AI intent.** AI and tool requests cannot bypass actuator
   limits, electrical or current limits, battery or thermal policy,
   emergency-disable state, or other runtime safety controls.
8. **Conversation is not a fixed turn cycle.** Do not assume a strict
   listen -> think -> speak -> listen sequence. When the interaction system
   permits it, the runtime should support overlapping listening and speaking,
   interruption or barge-in, tool work, and asynchronous cognition.
9. **Degrade gracefully.** Losing an AI or cloud connection should reduce
   capabilities, not stop the robot from functioning.
10. **Respect the Raspberry Pi Zero 2 W.** Keep local work focused on
    deterministic robot control, sensing, audio transport, state and event
    handling, safety, and lightweight computation. Heavy computation may run
    elsewhere when appropriate.
11. **Make operation observable.** Important state transitions,
    sensor-derived events, AI and tool requests, decisions, and physical
    actions should be inspectable through structured diagnostics or logging.
12. **Let implementation drive architecture.** Do not generalize a robotics
    framework around hypothetical robots. Mira is the first reference
    implementation and should reveal which abstractions are genuinely reusable.
