# Documentation guide

embodied-runtime is the reusable runtime; Mira is its reference robot.
Use this guide to find the document that owns a topic's detailed contract,
or choose a reading path for the task at hand.

## Start here

- **Run a first instance:** follow the [repository quick start](../README.md),
  then [startup and environment setup](startup.md) and [secrets](secrets.md).
- **Configure or deploy Mira:** read [configuration](configuration.md),
  [Mira profiles](mira-profiles.md), and the [startup/systemd guide](startup.md).
- **Understand the runtime:** begin with [design principles](design-principles.md),
  the [cognitive work map](cognitive-work.md), and [architecture](architecture.md).
  The map covers the cognitive layer; architecture covers the wider runtime.
- **Operate or investigate a running instance:** use the [console](console.md),
  [runtime diagnostics](runtime-diagnostics.md), and [run history](run-history.md).
- **Understand durable work:** start with [Jobs](jobs.md), then follow the
  workspace, context, and experiment references below.

## Setup and operation

| Topic | Detailed reference |
| --- | --- |
| Runtime configuration keys, defaults, validation, and precedence | [Configuration](configuration.md) |
| Python environment setup, startup wizard, and systemd operation | [Startup](startup.md) |
| Credential storage and delivery | [Secrets and API keys](secrets.md) |
| Reference deployment model choices and operating policy | [Mira profiles](mira-profiles.md) |
| Operator commands, input editing, and console diagnostics | [Console](console.md) |
| Bounded operational inspection tools and their authority | [Runtime diagnostics](runtime-diagnostics.md) |
| Per-process run records, retention, and inspection | [Run history](run-history.md) |
| Managed ngrok ingress policy, setup, and live acceptance | [ngrok ingress](ngrok-ingress.md) |
| Provider-reported cost accounting and estimate precedence | [OpenRouter cost accounting](openrouter-cost-accounting.md) |

## Architecture and runtime boundaries

| Topic | Detailed reference |
| --- | --- |
| Durable project constraints | [Design principles](design-principles.md) |
| Component ownership, attention admission, resource arbitration, and episode grammar | [Architecture](architecture.md) |
| Relationships between cognitive work, evidence, memory, artifacts, and communication | [Cognitive work map](cognitive-work.md) |
| Authoritative state versus goals, history, and observations | [Runtime state](state.md) |
| Semantic events and their projections | [Events](events.md) |
| Semantic body interface | [Body boundary](body.md) |
| Deterministic local behavior | [Reflexes](reflexes.md) |
| One-shot session-local commitments | [Temporal follow-up](temporal-followup.md) |
| Passive semantic inspection areas and acquisition behavior | [Self-inspection](self-inspection.md) |

## Jobs, memory, and retained context

These sources have different lifetimes and authority. Durable storage does not
make authored material fresh runtime evidence; follow the individual contracts.

| Topic | Detailed reference |
| --- | --- |
| Job/JobRun lifecycle, outcomes, continuation, scheduling, triggers, and execution contexts | [Jobs](jobs.md) |
| Job-owned text artifacts, access, containment, and mutation | [Job Workspaces](job-workspaces.md) |
| Cross-Job historical text and result access | [Shared reads](job-shared-reads.md) |
| Carry-forward focus records and administration | [Job focus](job-focus.md) |
| Published claims, evidence provenance, and deliberate search | [Findings](findings.md) |
| Entities, durable semantic knowledge, admission, and recall | [Persistent memory](persistent-memory.md) |
| Retained operator dialogue, request identity, work links, and delivery evidence | [Conversation history](conversation-history.md) |

## Cognition, interaction, and images

| Topic | Detailed reference |
| --- | --- |
| AI integration overview and implemented versus candidate approaches | [AI stack](ai-stack.md) |
| OpenAI Responses setup and adapter behavior | [OpenAI cognition](cognition-openai.md) |
| Compatible Chat Completions and OpenRouter setup | [Compatible cognition](cognition-compatible.md) |
| Channel identity, dialogue versus delivery, and routing policy | [Interactions](interactions.md) |
| SMS/MMS transport setup and bounds | [SMS and MMS](sms.md) |
| Bounded speech sessions, wake listening, STT, and TTS | [Voice conversation](voice-conversation.md) |
| Visual interpretation, provider chains, fallback, and acquisition authority | [Visual perception](visual-perception.md) |
| Joint image analysis and retained operator-image recall | [Conversation images](conversation-images.md) |
| Job-owned image storage, provenance, interpretation, and delivery | [Workspace images](workspace-images.md) |

## Hardware

| Topic | Detailed reference |
| --- | --- |
| Fusion HAT+ backend selection, provisioning, and verified hardware facts | [Fusion HAT+](hardware/fusion-hat-plus.md) |
| CSI camera provisioning, sensor coverage, and physical verification | [Raspberry Pi CSI camera](hardware/raspberry-pi-csi-camera.md) |

## Experiments, examples, and validation

These pages describe opt-in experiments, exercised patterns, or validation
procedures. They do not imply that a fresh checkout installs the example Jobs
or that every documented procedure has passed on a physical robot.

| Purpose | Guide |
| --- | --- |
| Examples of exercised Job compositions and their validation labels | [Tested Job patterns](tested-jobs.md) |
| Opt-in procedure learning, review, and activation | [Job procedures](job-procedures.md) |
| Bounded reflection using historical cross-Job evidence | [Reflective continuity](reflective-continuity.md) |
| Offline cognition contract scenarios and provider comparisons | [Cognition benchmarks](cognition-benchmarks.md) |
| Operator checks for fixed-image interpretation, fallback, and camera use | [Portable vision validation](portable-vision-validation.md) |

## Keeping the references coherent

Use the topic homes above for detailed behavior. Overview pages and walkthroughs
should summarize and link to those contracts rather than maintain independent
copies of limits, defaults, or lifecycle rules. Configuration owns key/default
reference; the relevant feature page owns what those settings mean in operation.

When behavior changes, update its detailed reference and any affected overview
or example. Preserve unique rationale, edge cases, and validation evidence, and
label historical or experimental statements explicitly. If documents disagree,
check the implementation and relevant tests before resolving the wording.

Contributor instructions remain in [AGENTS.md](../AGENTS.md).
