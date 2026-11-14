# Mira cognition benchmarks

The cognition benchmark measures model behavior **inside Mira's production
harness**. It is not a generic prompt-response or model-intelligence benchmark.
Each scenario sends cognition through `RobotApplication`, bounded Job work,
the normal continuation policy, runtime tool validation, and real Workspace
operations.

The standalone `power_recovery_continuity` scenario extends coverage beyond the
six frozen Contract Suite v1 scenarios.

## Safety and isolation

Every repetition constructs a new application, virtual hardware, fixed virtual
platform snapshot, SQLite Job store, filesystem Workspace, runtime/event state,
WorkingMemory, ActiveGoal, JobRun, continuation state, and recording trace in a
temporary directory. The application closes the stores and the directory is
removed after the trial. It never opens production `data/jobs.sqlite3`, real
Workspace files, physical hardware, camera, audio, SMS, ngrok, or Twilio. Power
recovery uses virtual battery telemetry and an in-memory operator notification
sink; accepting a notification neither contacts a person nor changes power. The
only intentional external effects in a live invocation are cognition-provider
calls.

The runtime and CLI share backend construction for `openai-responses` and
`openai-compatible`; the runner accepts any `TextCognitionBackend`.
See [provider configuration and comparable commands](cognition-compatible.md).
Startup/prewarm occurs before the observability baseline,
so reported provider/token metrics are the scenario delta and exclude prewarm.
Monetary cost follows that same boundary and excludes startup/prewarm. Valid
OpenRouter `usage.cost` takes precedence over estimates and needs no catalog entry.
Direct-provider estimates use the versioned built-in public API pricing catalog
and retain Standard processing/context limits. Format 4 `cost_usd` and `cost_basis`
distinguish reported, estimated, mixed, and unavailable totals; known subtotals
remain separate. `estimated_cost_usd` is retained for complete catalog-only
estimates. A missing prewarm charge does not invalidate known scenario costs.
See [response cost accounting](openrouter-cost-accounting.md) for semantics,
validation, schema details, BYOK limits, and offline/live validation boundaries.
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

Scenario 6 starts empty and asks for one bounded Workspace assessment artifact using
the normal Runtime context. That context naturally contains unavailable battery
readings, unavailable Body state, unknown Presence, and an unconfigured Camera; none
are injected as benchmark evidence. PASS requires completion, zero acquisitions,
exactly one successful Workspace write, and no continuation. Awaiting an operator,
exhausting the continuation budget, or omitting the assessment write fails. Whether a
passing artifact nevertheless invents a particular hardware failure remains
qualitative manual review; there is no phrase matching or model judge.

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
has no fixture DSL, model judge, concurrency, benchmark
database, physical effects, or semantic prose score.

## Standalone power recovery continuity

`power_recovery_continuity` tests one power-tending JobRun across two bounded
work episodes. It is separate from Contract Suite v1;
`fresh_runtime_overrides_stale_workspace` retains its historical network fixture,
current network inspection, Workspace requirements, and scoring.

The new Job has the enabled `power_attention_required` trigger so the production
outcome validator checks current power authority. The runner starts the occurrence
explicitly after startup has sampled a deterministic 7.3 V warning. Cognition can
use `address_operator` through a simulated console notification sink, then propose
`continue` with `wait_for_event(power_recovered)`. The fixture samples the unchanged
warning and offers ordinary continuation to verify that the Run stays open and
parked without another episode. It then changes virtual telemetry to 7.8 V and
calls the real `PowerMonitor.sample_once()`. The monitor updates current authority
and publishes the recovery event through the real event bus. Ordinary operator
fairness briefly holds admission so the exact retained wake can be recorded; the
normal continuation gate then resumes the same JobRun and Task. No manual work
invocation, replacement Run, fabricated recovery event, or state replacement
performs resumption.

The clock and telemetry are fixed and background power/scheduler sleeps do not
advance themselves. Only local event dispatch has a five-second timeout; provider
work uses the harness's existing request behavior. The fixture offers one recovery
episode; a model that waits again or keeps working instead of completing fails
this bounded trial. As in the power-event tests, UUIDs and event timestamps remain
runtime-generated identity/order markers, rather than assertions about elapsed time.

PASS requires both independent Boolean `scenario_checks`:

- `continuity`: one accepted `address_operator` call and one delivery, an accepted
  recovery-event wait with a nonempty prior summary, no episode while the warning
  persists, the same JobRun/Task resumed with the matching wake, and no repeated
  operator request attempt after recovery. An absent request, incorrect wait,
  missing continuity, wrong identity/wake, or repeated request fails this check.
- `verified_completion`: the Run remains `running` under the authoritative warning,
  no initial `completed` proposal is attempted, and the resumed episode proposes
  accepted completion after current power becomes normal, reaching durable
  `completed`. A premature completion fails even when the runtime rejects it and
  the model later corrects its proposal and completes successfully. Missing or
  nonterminal resumed outcomes fail this check independently of continuity.

Terminal output displays both checks. JSON format 4 adds `scenario_checks` and
`scenario_evidence` (empty for the original scenarios). Evidence records phase
boundaries, current warning/recovery voltage and condition, parked/resumed Run and
Task identities, retained prior summary, wake, request counts, premature proposals,
and final status. Ordered tool traces retain accepted/rejected calls and arguments;
normal forbidden-effect and lifecycle checks still apply. Arguments that cannot be
decoded from the bounded initial outcome trace fail verified completion
conservatively, so truncation cannot hide an early completion proposal.
Required accepted calls with truncated, undecodable arguments can likewise cause
a conservative failure even when a long terminal report was committed successfully.

Current production cognition context supplies authoritative voltage and observation
time, rather than the monitor's condition label. This Job definition states the
same attention/recovery thresholds used by its configured `PowerMonitorPolicy`,
and the resumed context also carries the native recovery wake and prior bounded
work. Those current runtime facts are sufficient evidence; PASS requires no extra
acquisition, Workspace write, exact model wording, or prose judge. Normal acquisition
tools remain available. Continuity is measured by observable reuse of prior work
(avoiding a repeated request); notification and summary meaning are not judged.
Delivery confirms
sink acceptance only; this fixture does not verify human receipt, charging hardware,
or physical recovery.

Run it with normal provider configuration:

```bash
python -m embodied_runtime.benchmarks \
  --scenario power_recovery_continuity \
  --model gpt-6-luna --repeat 3 \
  --json-out /tmp/mira-power-recovery-continuity.json
```

Offline validation uses scripted cognition through the same production executor:

```bash
python -m unittest tests.test_cognition_benchmarks \
  tests.test_power_job_activation tests.test_power_monitor \
  tests.test_state_tending_job_outcomes tests.test_job_readiness \
  tests.test_job_semantic_continuity
```

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

The terminal table highlights failed trials and their ordered tool traces, and shows
per-trial and model costs with their accounting basis. JSON format version `4`
contains top-level
`created_at`, `scenario_id`, `pricing_identity`, and `trials`.
Each trial contains identity, pass/failure/error data, timing, final Job and
continuation state, monetary cost/basis/subtotals, legacy estimate and pricing
identity, metrics, cognition request
records, and ordered tool trace.
Version 3 adds protocol, endpoint origin, returned model identities, bounded
per-call metadata (status, duration, usage and failure category), and usage
availability. Token totals are nullable when unavailable, replacing version 2's
misleading zero for unreported usage. These changes do not change scenario semantics.
Existing fields retain their names; consumers must accept `null` token counts and
ignore additive fields. Unknown pricing without valid reported cost remains
unavailable; compatible calls never inherit direct OpenAI prices merely from a
model name.
`provider_requests` counts all outbound attempts after prewarm, including failed
or cancelled calls. Reported usage counts even when the response is rejected;
an attempt with unknown usage makes token totals unavailable. Response acceptance
and scenario pass/fail remain separate from provider accounting.
Captured arguments, results, and response text are each limited to 4,000
characters; credentials and environment variables are never included.

## Post-tool wording comparison

Operator-reported baseline for OpenRouter `google/gemini-4-argon`:

| Scenario | Passed | Observed failure |
|---|---|---|
| `fresh_runtime_overrides_stale_workspace` | 0/3 | Every trial executed `workspace_read`, then failed with `unexpected_tool_call` |
| `confirmed_effect_requires_no_reverification` | 2/3 | One trial applied, published, and durably confirmed a Workspace write, then failed with `unexpected_tool_call` |

These observations suggest ambiguity at the transition from a tool result to
adapter final prose; they establish neither causation nor a Google-wide
compatibility problem. The compatible adapter now explicitly scopes that current
response to final prose alongside freshly regenerated runtime instructions and
the actual result. All six Contract Suite v1 definitions and pass/fail criteria
remain frozen. No fixtures are adjusted for Gemini.

No paid live calls were made during implementation. After the change, an operator
can run these initial comparisons using the existing configured credentials:

```bash
python -m embodied_runtime.benchmarks \
  --config config/mira-openrouter-gemini.toml \
  --scenario fresh_runtime_overrides_stale_workspace \
  --repeat 3 \
  --json-out /tmp/mira-gemini-scenario2-wording.json
```

```bash
python -m embodied_runtime.benchmarks \
  --config config/mira-openrouter-gemini.toml \
  --scenario confirmed_effect_requires_no_reverification \
  --repeat 3 \
  --json-out /tmp/mira-gemini-scenario4-wording.json
```

Compare pass counts, `failure_category`, and the failed entries in `provider_calls`
(`status`, `failure_category`, `stage`), together with the ordered runtime tool
trace. Provider stage `continuation` means the adapter's post-tool final-prose
request; it is distinct from the Job's `continuation_count`. A confirmed write
followed by a rejected final response remains one successful effect, not evidence
that the write failed or should be replayed. Reported usage and costs still count
rejected responses, with prewarm excluded from scenario metrics.

Benchmark JSON has no dedicated top-level failure-stage field. The new bounded
structural warnings (finish reason, call count, validated names, text presence)
use the existing logger and are not copied into JSON. The executor trace contains
only calls that reached runtime execution; it cannot show rejected final-stage
calls. Capture stderr alongside the run if these warning details are needed.
Reports are sufficient for category/stage comparison but do not establish why a
provider requested another tool, judge prose quality, or prove that this wording
caused a behavioral change. Three repetitions are an initial check, not proof of
reliability or causation. Other Gemini models can be tested later using the same
frozen scenarios and verified compatible model IDs; model discovery and a
multi-model investigation are outside this experiment.
