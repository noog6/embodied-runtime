# Tested Job patterns

This gallery records useful Job patterns exercised during development of Mira.
They are examples of compositions that the generic runtime has successfully
supported, not exact deployable definitions or normative responsibilities for
another embodied agent. A fresh checkout does not install, create, or enable any
of these Jobs. Names and responsibilities belong to the reference deployment;
Job numbers, where a deployment assigns them, are database-local identities and
are not portable names.

```text
embodied-runtime
    supplies generic Job mechanisms

Tested Job patterns
    document useful compositions that have been exercised

Mira's local Job catalog
    belongs to the reference robot / deployment
```

The detailed contracts remain in [Jobs](jobs.md), [Job
Workspaces](job-workspaces.md), and [Findings and deliberate
search](findings.md). See also [runtime diagnostics](runtime-diagnostics.md),
[events](events.md), and [visual perception](visual-perception.md) for the
authority and resource boundaries composed by some patterns below. This page
answers “what compositions have we exercised?” rather than restating how those
mechanisms work.

## Validation vocabulary

- **Automated** — the relevant runtime mechanisms are exercised by the automated
  test suite. This does not mean that an exact local Job definition is an
  automated fixture.
- **Runtime tested** — the pattern has been exercised manually in a running
  embodied-runtime environment.
- **Physical** — the pattern has been exercised on the physical Mira reference
  robot.

These labels describe development evidence, not certification, maturity, or a
requirement that another deployment use the same composition.

## Gallery

### Runtime Self Check

**Responsibility:** Perform a bounded review of available runtime state and
capabilities and report meaningful issues.

**Typical activation:** Explicit operator start or a deployment-configured
activation.

**Runtime features exercised:**

- durable Job and JobRun lifecycle
- bounded runtime inspection and acquisitions
- outcome evaluation and durable JobRun occurrence

**What this pattern demonstrates:** A small diagnostic responsibility can use
the ordinary durable Job machinery instead of gaining a separate diagnostic
scheduler or execution system. Its authored interpretation remains historical
output; current diagnostics remain authoritative for current state.

**Validation:** Automated mechanisms + Runtime tested.

### Nightly Self Log Reviewer

**Responsibility:** Review a bounded portion of recent runtime history on a
daily schedule and summarize notable information available through the
configured runtime interfaces.

**Typical activation:** A configured daily local-time Job schedule.

**Runtime features exercised:**

- daily scheduled activation
- durable, independent JobRun occurrences
- bounded historical review
- outcome evaluation and JobRun result report

**What this pattern demonstrates:** A schedule starts a new occurrence, rather
than continuing an earlier cognition session. Each review has its own JobRun and
bounded report. The pattern does not imply unrestricted filesystem or log access;
it can review only history exposed by the runtime's configured, bounded
interfaces.

**Validation:** Automated mechanisms + Runtime tested.

### Tend to my power

**Responsibility:** Respond when runtime-owned power monitoring indicates that
attention is required, while remaining grounded in authoritative current power
state.

**Typical activation:** The semantic `power_attention_required` trigger, with a
later `power_recovered` event able to satisfy an event wait; an operator may also
start the Job explicitly.

**Runtime features exercised:**

- semantic event-triggered activation and recovery-event semantics
- runtime-owned current-state authority
- bounded continuation and event waiting
- operator involvement and grounded outcome evaluation

**What this pattern demonstrates:** A deterministic low-level monitor can own
sensing, thresholds, and state transitions while a higher-level Job owns bounded
interpretation and response. Cognition neither chooses battery thresholds nor
manufactures recovery; asking an operator to connect power does not itself prove
that power recovered.

**Validation:** Automated mechanisms + Runtime tested.

### Tend to my runtime health

**Responsibility:** Review computational and runtime health when relevant
conditions occur and report what warrants attention.

**Typical activation:** A configured semantic thermal or memory-health trigger,
or an explicit operator start.

**Runtime features exercised:**

- runtime and platform-health inspection
- semantic health events
- bounded acquisitions, continuation, and waiting
- grounded outcome evaluation

**What this pattern demonstrates:** Deterministic monitors retain ownership of
health sampling and condition transitions while a Job provides bounded
higher-level stewardship. The pattern reports and reasons about supported
evidence; it does not imply an automatic repair capability.

**Validation:** Automated mechanisms + Runtime tested.

### Tend to my capabilities

**Responsibility:** Maintain a grounded understanding of currently available
capabilities.

**Typical activation:** Explicit operator start or a deployment-configured
activation.

**Runtime features exercised:**

- fresh authoritative runtime inspection
- historical Job Workspace baseline and Workspace updates
- bounded Job cognition and outcome evaluation
- Finding publication, completed-run visibility, and later deterministic
  Finding context selection

**What this pattern demonstrates:** A Job can compare durable working material
with fresh runtime evidence, update its Workspace, and optionally publish a
reusable historical claim without presenting historical material as current
truth. A demonstrated information path is:

```text
Job reviews capabilities
        ↓
compares fresh runtime evidence with historical Workspace material
        ↓
may publish a Finding
        ↓
source JobRun completes
        ↓
Finding becomes reusable
        ↓
later operator question
        ↓
deterministic librarian may select the historical Finding
        ↓
cognition may still perform fresh inspection
```

The authority distinction is essential: a Workspace is historical working
material; a Finding is a reusable historical Job-authored claim; fresh runtime
inspection is current authority. A Finding is visible for reuse only after its
source JobRun completes successfully, and selection does not make it current.

**Validation:** Automated mechanisms + Physical Mira runtime.

### Tend to my embodiment

**Responsibility:** Review the currently represented embodiment and relevant
hardware, body, and camera state.

**Typical activation:** `runtime_ready` or explicit operator start.

**Runtime features exercised:**

- bounded current runtime and capability inspection
- available hardware, body, and camera evidence
- grounded outcome evaluation

**What this pattern demonstrates:** An embodied agent can maintain a bounded
responsibility concerning its physical and runtime embodiment without assuming
unavailable body state. Missing or unsupported evidence remains unknown rather
than being filled in from a description or old Workspace material.

**Validation:** Automated mechanisms + Runtime tested.

### Tend to my communications

**Responsibility:** Review current communication readiness across configured
communication capabilities.

**Typical activation:** Explicit operator start or a deployment-configured
activation.

**Runtime features exercised:**

- bounded runtime-health, configuration, and capability inspection
- bounded comparison with available historical material
- bounded completion and outcome evaluation

**What this pattern demonstrates:** One durable responsibility can summarize
several related communication capabilities without turning every subsystem into
an autonomous loop. Configuration or local readiness is not proof that an
external message was delivered successfully.

**Validation:** Automated mechanisms + Runtime tested.

### Tend to my surroundings

**Responsibility:** Maintain a small, grounded understanding of the immediate
surroundings from available perception and presence evidence.

**Typical activation:** Explicit operator start or a relevant
deployment-configured activation.

**Runtime features exercised:**

- physical camera acquisition under a `ResourceArbiter` camera lease
- visual perception and bounded acquisitions
- grounded uncertainty and outcome evaluation

**What this pattern demonstrates:** A Job can acquire fresh sensor evidence,
report only what is visible, preserve uncertainty about unseen areas, and
complete without inventing a perpetual observation loop. The camera lease is
runtime authority, not authority owned by the Job definition.

**Notes:** This pattern became clearer when its description stated the
responsibility and completion intent rather than encoding a prose state machine.
Tested stewardship Jobs generally work best when their descriptions define what
they own while the runtime supplies acquisition budgets, effects, lifecycle,
continuation, waiting, resource arbitration, authority rules, and Finding
semantics.

**Validation:** Automated mechanisms + Physical Mira runtime.

## Development context and boundaries

Physical development runs have exercised multiple stewardship Jobs concurrently,
including startup-triggered Jobs, while operator cognition remained independently
available. That is architectural evidence that these compositions are not merely
isolated pseudocode; it does not turn Mira's catalog into generic runtime policy.

This first gallery intentionally omits synthetic and narrowly constructed test
recipes such as **Two Presence Changes**, **Two Knock Phase 5C**, **Presence
Gate**, **Wait for Purple Object**, and **Workspace Smoke Test**. They remain
useful development fixtures, but a future test-recipes section would be a better
home for them.

The natural-language “Tend to my …” names reflect Mira's local catalog. Other
robots need not use anthropomorphic names, these responsibilities, the same
triggers or baselines, or Findings at all. What is reusable is the generic
composition of a durable responsibility with runtime-governed execution and
authority boundaries.

## Future: portable Job examples

A possible future extension is to turn selected tested patterns into portable,
explicitly importable Job definitions. Such examples could let operators opt in
to a known composition without making it part of the runtime's default Job
catalog. Importable examples, if added, should remain opt-in compositions built
on the generic runtime, not hidden built-in behavior.

No import format, import command, or automatic installation is defined today.
