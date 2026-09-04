# Architecture

The camera layer owns one-shot encoded capture. The visual perception layer can
interpret one transient frame on deliberate request; application orchestration
owns validation, capability projection, and autonomous request bounds. See
[`visual-perception.md`](visual-perception.md).

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
Camera acquisition is now a focused application-owned sensing resource, documented
in [Raspberry Pi CSI camera](hardware/raspberry-pi-csi-camera.md). Its encoded
frames remain transient resources rather than state or events. The implemented
boundary is camera acquisition -> future perception -> future semantic observation;
only that future semantic observation would enter state or the event mechanism.
The [local runtime console](console.md) is a projection of that state and a thin
adapter for explicit development input, not an additional state owner or observer.

Startup configuration is a separate launch-owned seam:

```text
strict TOML -> validated file values -> historical defaults + explicit CLI
            -> existing backend selection and ApplicationOptions
```

The loader neither constructs backends nor enters `RobotApplication`; application
policy remains represented by `ApplicationOptions`. Configuration is read once
before startup, and final dependency validation follows the merge. See
[Runtime startup configuration](configuration.md).

The narrow grounded cognition path is implemented as:

```text
RuntimeState / application summaries
              |
              v
  selected cognition projection
              |
operator text v
       RobotApplication -> TextCognitionBackend -> provider -> text response
```

`RobotApplication` remains the ownership boundary. It creates a fresh,
immutable, explicitly allow-listed projection immediately before each request;
that projection is not a second state owner. Phase 3 adds one explicitly
projected request-time capability with this ownership path:

```text
model orient_body request
          |
          v
runtime cognition dispatcher
          |
          v
RobotApplication.set_body_orientation()
          |
          v
BodyBackend -> authoritative RuntimeState
```

The provider adapter transports the request and runtime-produced result but
never owns a body backend or state. The application validates availability and
untrusted arguments, invokes its semantic capability, and refreshes the
authoritative projection for the final response. Context, requests, tool-call
identifiers, and responses are neither state nor events.

For cognition continuity and commitment, `RobotApplication` separately owns
current truth, current intention, and bounded history:

```text
RobotApplication
  |- RuntimeState       current authoritative truth
  |- ActiveGoal         current intentional state (zero or one)
  |- WorkingMemory      bounded operator cognition history
  |- Attention          deterministic relevance gate

RuntimeState ---------\
ActiveGoal ------------\
WorkingMemory ----------> cognition request
operator request ------/
```

`WorkingMemory` is a volatile FIFO of completed cognition interactions. It is
neither part of `RuntimeState` nor EventBus history, and unrelated state changes
do not create memory turns. Each request receives a fresh current-state
projection plus a snapshot of memory that predates that request.

`ActiveGoal` is a separate volatile, immutable description of the application's
single current commitment. It is neither physical truth nor conversation
history. Establishing it causes no action, and actions or reflex-driven state
changes neither satisfy nor clear it. Without explicitly enabled initiative,
cognition runs only on an explicit ask.

Small deterministic [local reflexes](reflexes.md) may consume semantic events
and request semantic application capabilities. The implemented path is sensing
or semantic observation -> authoritative state and event -> reflex -> semantic
capability -> body. A reflex neither owns state nor accesses a body backend
directly, and configured reflex lifecycles belong to the application.
Reflexes and cognition remain independent even though both use the same
application-owned semantic capability; future cognition sees any pose a reflex
subsequently establishes.

With opt-in initiative, one narrow path is transition-driven:
`BodyOrientationChanged` from a local reflex -> attention gate, plus an existing
`ActiveGoal` -> optional one-shot cognition. Attention only selects semantic
events, owns the one-in-flight lifecycle, cancellation, and volatile diagnostics.
`--initiative` permits bounded read-only acquisition where available and the
single `schedule_followup` semantic effect. Independent effect permissions project
`orient_body` for a nonphysical body and/or `address_operator` when an operator
message sink is configured. Without the Phase 10 continuation opt-in, the application accepts at most one
capability request total, even if both are offered.

For outward initiative, cognition chooses only bounded message text.
`RobotApplication` validates it and delivers an immutable
`OperatorMessage(text, source="initiative")` through a provider-neutral sink. The
local console is today's concrete transport; speech, web, and mobile transports
are possible replacements but are not implemented. Recipient, source, routing,
and presentation remain runtime-owned. Questions do not wait for or correlate a
reply.

Autonomous responses, messages, tool results, and outcomes enter neither
WorkingMemory nor RuntimeState and create no history. With explicit
`--initiative-goal-closure`, one requested effect may receive one independent
outcome evaluation using fresh reality and the same request-local goal and memory
snapshot. An applied effect may expose only `complete_goal`, guarded by same-goal
identity; a rejected effect is evaluated read-only. Without continuation, no second effect is available; retry, planning, pursuit
loops, and goal replacement remain unavailable in every mode.

Phase 10 optionally extends application orchestration—not attention or the
provider adapter—with one bounded continuation:

```text
attention -> independent Initiative A -> optional effect #1
          -> fresh runtime -> independent Continuation B -> optional distinct effect #2
          -> fresh runtime -> optional independent Outcome C -> stop
```

`--initiative-continuation` requires both current effect permissions. The
application snapshots WorkingMemory once, captures the exact ActiveGoal object,
and reuses both across A, B, and C while rendering fresh RuntimeState for each.
B is eligible only after an applied first effect, while running with that same
goal, and only when a different capability is freshly available. Each request
accepts at most one call; B cannot repeat A's tool. Outcome grounding contains
one or two effect results and exposes completion only if all requested effects
applied. Attention still owns event selection, one in-flight task, cancellation,
and volatile diagnostics; `RobotApplication` owns projection, validation,
execution, identity checks, and ordering. There is no planner, loop, retry,
persistent memory, or provider session.

## General semantic observations

Phase 11 projects selected authoritative events into immutable, request-local
`SemanticObservation` values before goal-directed attention runs. Reflex-sourced
body orientation changes remain enabled by `--initiative`; thermal-warning and
memory-pressure raised/cleared transitions additionally require
`--initiative-platform-attention`. The event says what transitioned, fresh
`RuntimeState` remains authoritative, and the observation only says why this
request occurred. No observation is stored, queued, polled, or added to working
memory. `PresenceChanged` is intentionally excluded because its centering reflex
already produces the richer body transition. Phase 10 continuation and outcome
orchestration are independent of observation kind and gain no new effect tool.

## Bounded semantic self-inspection

Bounded situational facts use the single runtime-owned `inspect_self(area)`
capability described in [Bounded semantic self-inspection](self-inspection.md).
It increases request-local information, not authority, state, events, or effect
budgets.

## Bounded temporal follow-up

Phase 15 adds one app-owned `TemporalFollowupController` beside—not inside—runtime
state, goals, or memory. It owns at most one goal-identity-bound monotonic timer
and publishes a transient due event that enters normal attention. Fresh cognition
is created only when due; no old authority or provider request crosses the delay.
See [Bounded temporal follow-up](temporal-followup.md).
