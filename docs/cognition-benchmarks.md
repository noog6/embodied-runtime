# Mira cognition benchmarks

The cognition benchmark measures model behavior **inside Mira's production
harness**. It is not a generic prompt-response or model-intelligence benchmark.
The first scenario sends cognition through `RobotApplication`, bounded Job work,
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

## Scenario and interpretation

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

The terminal table highlights failed trials and their ordered tool traces. JSON
format version `1` contains top-level `created_at`, `scenario_id`, and `trials`.
Each trial contains identity, pass/failure/error data, timing, final Job and
continuation state, metrics, cognition request records, and ordered tool trace.
Captured arguments, results, and response text are each limited to 4,000
characters; credentials and environment variables are never included.
