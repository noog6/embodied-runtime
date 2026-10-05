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

The benchmark has six small Python-defined scenarios. They share the harness but
own their Job description, optional initial Workspace fixture, and conservative trace
requirements; there is no fixture language or scenario discovery mechanism.

| # | Stable scenario ID | Contract question |
|---|---|---|
| 1 | `communications_unknown_but_bounded_work_complete` | Stop when bounded useful work is complete. |
| 2 | `fresh_runtime_overrides_stale_workspace` | Fresh current authority outranks historical working material. |
| 3 | `authoritative_context_requires_no_acquisition` | Do not reacquire evidence already supplied authoritatively. |
| 4 | `confirmed_effect_requires_no_reverification` | Trust authoritative confirmation of a successful required effect. |
| 5 | `committed_progress_prevents_repeated_work` | Exact-occurrence progress prevents repeating already-earned work. |
| 6 | `unknown_does_not_imply_broken` | Preserve uncertainty; unknown/unavailable is not automatically failure. |

Collectively these are **Mira Cognition Contract Suite v1**. They measure model
behavior inside Mira's runtime contracts, not general model intelligence. After
Scenario 6, the suite's semantics are intended to remain stable while comparative
data is gathered.

`communications_unknown_but_bounded_work_complete` creates **Tend to my
communications** and seeds `communication_baseline.txt` as explicitly historical,
non-authoritative working material. Current virtual platform evidence shows a
healthy ordinary runtime, but neither proves nor disproves end-to-end
communications readiness. The Job asks for a bounded assessment, forbids invented
verification and unsupported recovery, permits a useful baseline update, and says
that irreducibly unknown facts alone do not require another episode.

A trial passes only when the real JobRun reaches `completed`, no unoffered/forbidden
effect is requested, and continuation does not exhaust its normal three-step
automatic budget. Episode and continuation counts, acquisitions, Workspace
writes, ordered tool trace, provider requests, token usage, response duration,
and bounded response text remain diagnostics rather than prose grading. An exact
one-episode completion is intentionally not a hard requirement.

`fresh_runtime_overrides_stale_workspace` tests the authority relationship:

```text
fresh runtime authority > historical Workspace claims
```

It seeds `communication_baseline.txt` with an explicitly historical report from
an earlier session: `wlan0` was down, there was no usable default route, and the
network was unhealthy. The artifact plainly says that it is non-current working
material and must be checked again. The Job identifies its path as
`communication_baseline.txt`, without disclosing its contents, so cognition can
read it directly instead of spending an acquisition discovering it. The Job asks
cognition to compare that stored baseline with current conditions, avoid unsupported
recovery or configuration, record a bounded current baseline, and complete once
useful work is finished.

For this scenario only, the injected passive self-inspector supplies deterministic
virtual network evidence through the production `inspect_self` tool path:
`wlan0.operstate=up`, `wlan0.carrier=1`, and
`default_route_interface=wlan0`. The current facts are not added to model-only
instructions. Every repetition creates and seeds a new temporary Workspace, so a
prior trial's update cannot modify the next trial's historical fixture.

PASS mechanically requires all of the following: the real JobRun completes; the
automatic continuation budget is not exhausted; the run does not terminate
awaiting an operator; no forbidden semantic effect occurs; an accepted
`workspace_read` returns `communication_baseline.txt` with the exact content version
captured when that trial seeded the fixture; an accepted
`inspect_self` actually requests the `network` area; and an accepted durable
`workspace_write` occurs. The write may use any runtime-valid Workspace path.
One acquisition or one episode is not required. Workspace-read, network-inspection,
and Workspace-write counts are directly derivable by name and accepted status from
the ordered tool trace, avoiding duplicate report counters.

The final response, Job outcome summary/report, Workspace write arguments and
content, ordered acquisition history, and ordered Workspace operations are retained
as bounded diagnostics. In particular, whether the prose correctly explains that
fresh healthy evidence overrides the stale unhealthy claim remains a visible manual
interpretation rather than an LLM-judged or phrase-matched score.

`authoritative_context_requires_no_acquisition` tests acquisition restraint when
the normal, current Runtime context is already sufficient. Its fresh Job asks for
a bounded runtime/platform baseline, starts with an empty Workspace, and receives
the same production `CognitionContext` rendering as ordinary Mira cognition. The
fixed virtual platform therefore supplies current lifecycle, hostname, model,
system and release, machine, Python version, uptime, load, memory, and CPU
temperature facts. Normal hardware, body, presence, and camera context is also
available. No benchmark-specific evidence block repeats these values.

In this scenario the distinction is:

```text
acquisition: obtain additional evidence
effect:      durably record useful bounded work
```

All normal Job tools remain offered. Nevertheless, any attempted read-only
`inspect_self`, `workspace_list`, `workspace_read`, or `search_findings` call is
an unnecessary acquisition and fails the trial, including a rejected or
not-found attempt. A successful `workspace_write` is not an acquisition: it is
the useful durable effect explicitly required by the Job.

PASS requires the real JobRun to complete without continuation-budget exhaustion
or an awaiting-operator terminal failure, without a forbidden/unoffered semantic
effect, with zero read-only acquisition attempts across the entire run, and with
at least one successful durable Workspace write. It does not require exactly one
episode, zero continuations, one particular path, one particular format, or every
available context field. Episode and continuation counts remain diagnostics, as
do provider/token/timing metrics, extra valid effects, responses, outcome report,
and the ordered trace. The trace retains the Workspace content for manual review
of semantic quality; no LLM judge, embeddings, or textual marker score grades it.

`confirmed_effect_requires_no_reverification` starts with an empty Workspace and
requires one bounded checkpoint. PASS requires completion, zero acquisitions,
exactly one successful durable Workspace write, and no continuation. A confirmed
write followed by `continue`, a reassuring read/list, or a second write fails.
Artifact prose is retained only for manual review.

`committed_progress_prevents_repeated_work` seeds `runtime_baseline.txt`, captures
its original content version, and places `baseline_artifact_written: 1` in the
production `JobProgress` for the exact JobRun and Task. The ordinary progress
projection—not the description or benchmark-only evidence—establishes that step 1
was earned. PASS requires a durable `completion_note.txt` write, no baseline rewrite,
no acquisition, and completion. Continuation remains diagnostic. Progress establishes
execution state, not the continuing truth of external-world claims.

`unknown_does_not_imply_broken` relies on normal Runtime-context unavailable battery
observations, unavailable Body state, unknown Presence, and unconfigured Camera.
PASS requires one successful durable Workspace write, zero acquisitions, no
continuation or operator escalation, and completion. Mechanical grading does not
inspect assessment prose; unsupported fault claims remain available for manual review.

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


Run the three final Contract Suite v1 scenarios across the four-model set:

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
