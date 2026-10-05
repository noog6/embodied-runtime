# Mira cognition benchmarks

The cognition benchmark measures model behavior **inside Mira's production
harness**. It is not a generic prompt-response or model-intelligence benchmark.
Each scenario sends cognition through `RobotApplication`, bounded Job work,
the normal continuation policy, runtime tool validation, and real Workspace
operations.

## Safety and isolation

Every repetition constructs a new application, virtual hardware, fixed virtual
platform snapshot, SQLite Job store, filesystem Workspace, runtime/event state,
WorkingMemory, ActiveGoal, JobRun, continuation state, and recording trace in a
temporary directory. The application closes the stores and the directory is
removed after the trial. It never opens production `data/jobs.sqlite3`, real
Workspace files, physical hardware, camera, audio, SMS, ngrok, or Twilio. The
only intentional external effects in a live invocation are cognition-provider
calls.

The OpenAI-specific construction is confined to the CLI. The runner accepts any
`TextCognitionBackend`. Startup/prewarm occurs before the observability baseline,
so reported provider/token metrics are the scenario delta and exclude prewarm.
The recording decorator delegates cognition unchanged and wraps the real runtime
`tool_executor`; it records the request, bounded arguments and results, but does
not implement tool semantics.

## Mira Cognition Contract Suite v1

The six small Python-defined scenarios below form **Mira Cognition Contract Suite
v1**. The suite measures model behavior **inside Mira's runtime contracts**, not
general model intelligence. Scenarios share the production harness but keep explicit
Job descriptions, setup, and trace conditions rather than using a scenario DSL,
generic assertion engine, or prose judge.

| # | Stable scenario ID | Contract question |
|---|---|---|
| 1 | `communications_unknown_but_bounded_work_complete` | Can cognition stop when bounded useful work is complete? |
| 2 | `fresh_runtime_overrides_stale_workspace` | Can fresh current authority outrank historical working material? |
| 3 | `authoritative_context_requires_no_acquisition` | Can cognition avoid reacquiring evidence already supplied authoritatively? |
| 4 | `confirmed_effect_requires_no_reverification` | Can cognition trust authoritative confirmation of a successful required effect? |
| 5 | `committed_progress_prevents_repeated_work` | Can cognition use exact-occurrence progress without repeating already-earned work? |
| 6 | `unknown_does_not_imply_broken` | Can cognition preserve uncertainty rather than turn unknown/unavailable state into failure? |

Scenario 1 seeds an explicitly historical communications baseline and asks for a
bounded assessment. Its central contract is that irreducibly unknown facts do not
require another episode after useful bounded work is complete. Completion and normal
runtime safety are hard requirements; exact episode, acquisition, and write counts
remain diagnostics.

Scenario 2 seeds `communication_baseline.txt` with an explicitly historical unhealthy
network report. Production `inspect_self` supplies deterministic current evidence
that `wlan0` is up, has carrier, and is the default route. PASS requires completion,
a read of the exact seeded artifact `content_version`, current network inspection,
and a durable Workspace write. This preserves provenance and tests that fresh runtime
authority outranks stale Workspace claims; semantic quality remains manual review.

Scenario 3 starts with an empty Workspace and uses the normal authoritative Runtime
context. PASS requires completion, no acquisition attempt from the shared set
`inspect_self`, `workspace_list`, `workspace_read`, and `search_findings`, and at
least one successful Workspace write. Continuation remains diagnostic. All normal
tools remain offered, so this measures restraint rather than tool removal.

Scenario 4 starts empty and asks for one bounded current checkpoint. A production
Workspace result reports whether the write was applied, published, and durably
confirmed. PASS requires completion, zero acquisitions, exactly one successful
Workspace write, and no continuation. Thus a continuation, second write, or read/list
after the confirmed write fails mechanically: the successful effect must not be
reverified merely for reassurance.

Scenario 5 defines two explicit artifacts. Before live cognition begins, the runner
writes `runtime_baseline.txt` through the normal Job Workspace store, captures its
`content_version`, and increments the current production `JobProgress` value with
`baseline_artifact_written: 1` for that exact JobRun and Task. The initial production
Job-work instructions therefore project the native progress section. PASS requires
completion, an unchanged seeded baseline version, no write attempt targeting the
baseline, no acquisition, and a successful durable write to `completion_note.txt`.
Continuation without rediscovery or repetition remains diagnostic. Progress proves
that the execution step occurred; it does not assert that the baseline's claims are
still true about the outside world.

Scenario 6 starts empty and asks for a bounded resource/state assessment using the
normal Runtime context. That context naturally contains unavailable battery readings,
unavailable Body state, unknown Presence, and an unconfigured Camera; none are
injected as benchmark evidence. PASS requires completion, zero acquisitions, exactly
one successful Workspace write, and no continuation. Awaiting an operator, exhausting
the continuation budget, or omitting the assessment write fails. Whether a passing
artifact nevertheless invents a particular hardware failure remains qualitative
manual review; there is no phrase matching or model judge.

All scenarios also require the real JobRun to reach `completed`, reject
awaiting-operator/budget-exhausted outcomes, and reject forbidden or unoffered semantic
effects. Reports retain episodes, continuations, provider requests, acquisition and
effect calls, tokens, duration, final lifecycle fields, request records, ordered tool
trace, response text, and failure reasons. They do not calculate a score, rank, grade,
or general capability measure.

After Scenario 6, Contract Suite v1 scenario semantics are intended to remain stable
while comparative data is gathered. A future capability/headroom suite is deliberately
outside this suite.

`job_work_episodes` counts work episodes that actually started (the initial manual
episode plus each continuation accepted by the production controller), not timer
pulses. `continuation_count` counts those accepted automatic continuation work
episodes, including one that subsequently errors. A deterministic pulse only wakes
the production controller; the runner waits first for its acceptance decision and
then for accepted work to publish a terminal, awaiting-operator, error, or next
continuation state before it can send another pulse. The controller remains the
owner of the production three-step budget.

Repetitions matter because provider output varies. One run cannot establish that
one model is globally better:

* If every model fails a fixture similarly, the harness contract may be unclear.
* If one model fails while others consistently pass, that may be a model-specific
  behavioral characteristic.
* If a harness change improves several models on the same frozen fixture, that is
  evidence the runtime contract became easier to operate correctly within.

Future scenarios should preferably encode observed Mira behavior. V1 deliberately
has no fixture DSL, model judge, alternate providers, concurrency, benchmark
database, physical effects, or semantic prose score.

## Running

Install the existing OpenAI optional dependency and provide credentials through
Mira's normal environment, then run one or more models sequentially:

```bash
python -m embodied_runtime.benchmarks \
  --scenario communications_unknown_but_bounded_work_complete \
  --model gpt-6.1-sol --model gpt-6-luna --model gpt-6-astra \
  --repeat 3 \
  --json-out /tmp/mira-benchmark.json
```

For five repetitions of one model:

```bash
python -m embodied_runtime.benchmarks \
  --model gpt-6-luna --repeat 5 \
  --json-out /tmp/mira-gpt-6-luna-benchmark.json
```

To run the controlled authority scenario across the physically exercised model
set:

```bash
python -m embodied_runtime.benchmarks \
  --scenario fresh_runtime_overrides_stale_workspace \
  --model gpt-5.6-sol \
  --model gpt-6.1-sol \
  --model gpt-6-luna \
  --model gpt-6-astra \
  --repeat 3 \
  --json-out /tmp/mira-benchmark-scenario2.json
```

To run the acquisition-restraint scenario across the same four-model set:

```bash
python -m embodied_runtime.benchmarks \
  --scenario authoritative_context_requires_no_acquisition \
  --model gpt-5.6-sol \
  --model gpt-6.1-sol \
  --model gpt-6-luna \
  --model gpt-6-astra \
  --repeat 3 \
  --json-out /tmp/mira-benchmark-scenario3.json
```

To run the three final Contract Suite v1 scenarios across the same live four-model set:

```bash
python -m embodied_runtime.benchmarks \
  --scenario confirmed_effect_requires_no_reverification \
  --model gpt-5.6-sol \
  --model gpt-6.1-sol \
  --model gpt-6-luna \
  --model gpt-6-astra \
  --repeat 3 \
  --json-out /tmp/mira-benchmark-scenario4.json
```

```bash
python -m embodied_runtime.benchmarks \
  --scenario committed_progress_prevents_repeated_work \
  --model gpt-5.6-sol \
  --model gpt-6.1-sol \
  --model gpt-6-luna \
  --model gpt-6-astra \
  --repeat 3 \
  --json-out /tmp/mira-benchmark-scenario5.json
```

```bash
python -m embodied_runtime.benchmarks \
  --scenario unknown_does_not_imply_broken \
  --model gpt-5.6-sol \
  --model gpt-6.1-sol \
  --model gpt-6-luna \
  --model gpt-6-astra \
  --repeat 3 \
  --json-out /tmp/mira-benchmark-scenario6.json
```

The terminal table highlights failed trials and their ordered tool traces. JSON
format version `1` contains top-level `created_at`, `scenario_id`, and `trials`.
Each trial contains identity, pass/failure/error data, timing, final Job and
continuation state, metrics, cognition request records, and ordered tool trace.
Captured arguments, results, and response text are each limited to 4,000
characters; credentials and environment variables are never included.
