# Opt-in Job procedure experiment

A procedure is a private suggested method for a class of work. It differs from a
Run **result** (what one occurrence produced), a **Finding** (a historical claim),
and **focus** (an unresolved question). It is bounded authored text guiding existing
cognition, not an executable skill, workflow, semantic-memory admission, permission,
schedule, reservation, or execution budget. No global search or cross-Job procedure
access is offered. Existing Jobs, including the nightly reviewer, remain opted out.

The cycle is **propose → operator review → enable → use → evaluate → revise**.
The setup-created read-only experiment can also compare an active and pending
version before requesting explicit console approval. It never promotes itself.
Completion establishes a candidate's source, not its correctness. Operator review
is a runtime fact; it does not establish current-world truth. Plumbing tests do not
demonstrate improved effectiveness.

## Representation and bounds

All existing and new Jobs default to `procedure_enabled=false`. An opted-in Job
has at most one active procedure and one pending initial/replacement candidate.
Authored instructions use seven plain text fields, with optional `basis` when
nominating from current runtime evidence:

| Authored field | Maximum Unicode characters |
| --- | ---: |
| `name` | 80 |
| `purpose` | 240 |
| `applicability` (including preconditions) | 400 |
| `required_capabilities` (existing capabilities only) | 300 |
| `steps` (suggested steps and conditional branches in prose) | 800 |
| `verification` | 400 |
| `stopping_conditions` (including defer conditions) | 400 |

Fields are nonempty. Limits apply before trimming; Unicode categories beginning `C` (including control, format, surrogate,
private-use and unassigned characters) are rejected, including newlines. Write short prose, using semicolons
for branches. There is no executable schema or semantic checker proving the text
is useful or correct. A complete rendered projection, including guidance and
provenance, must fit **24,000 characters**; individually valid fields can still
exceed that aggregate limit through JSON escaping and will be rejected. Content is
never silently truncated. Serialized authored content (including `basis`) must also
fit 20,000 JSON characters, validated before staging a nomination.

Runtime provenance is separate from `instructions`: owning Job ID, monotonically
increasing per-Job version, `model`/`operator` author, exact completed source Run
and finish time (nullable for operator seeds), model Task UUID/episode, creation
and activation times, optional validated evidence reference, and definition hash.
The stable selector is the pair **`JOB<n> PROC<version>`**, not `PROC` alone.
Candidate and active identities do not change on activation. Replacement never
rewrites the preceding version or any Run snapshot. Source references preserve
original times; reading or projecting does not refresh evidence age. An operator
seed's source reference does not prove its instructions appeared in that source.

An optional `basis` is one of `wake_event`, `acquisition_1`, `acquisition_2`,
`effect_1`, `effect_2`, validated against this episode's accepted runtime evidence.
It is stored as provenance with the source Run/episode, not as a copied payload.
Historical Workspace/image retrieval is not a current evidence basis. No reports,
transcripts, acquisition payloads, or full prompts are copied into procedures.

## Nomination and atomic commit

For opted-in occurrences only, the existing `report_job_outcome` schema gains
nullable `procedure_candidate` and `procedure_feedback`. Older callers may omit
both. There is no new model tool, provider request, reflection scheduler, or loop.
A candidate object has exactly the seven fields above plus `basis` (nullable).
Candidates should capture a reusable lesson rather than narrate an incident.

Only an accepted **completed** outcome may nominate. The harness derives Job, Run,
Task and episode from the exact live context and checks the bound Task/ActiveGoal,
cancellation, current policy, and definition. Proposals are staged inside the
outcome callback until the provider response succeeds. Provider failure after a
callback, cancellation (even if caught by the provider), stale/late callbacks,
failed/stopped/interrupted Runs and rejected outcomes publish nothing.

SQLite rechecks source ownership, opt-in occurrence capture, policy and definition
inside the terminal `BEGIN IMMEDIATE` transaction. Candidate, feedback and accepted
terminal result commit together. SQL/commit failure rolls them back. Existing Task
terminalization precedes persistence; an explicit terminal retry may save its
accepted result, but never resurrects a lost candidate or feedback. There are no
automatic persistence retries or interrupted-Run resumptions.

An existing pending candidate is preserved. The callback returns the bounded
`pending_exists` disposition while still accepting a valid completed outcome;
the final transactional disposition (`proposed` or `pending_exists`) is retained
with the source Run. A pending candidate created after callback validation is also
protected by the transaction. A clear disposition does not mean activation.

## Selection, authority and live disablement

Before Task activation, the runtime captures a durable procedure selection for
this exact pending Run. It selects only an enabled, definition-compatible active
version, or records that no method was selected. Snapshot persistence failure
uses existing unstarted-Run compensation, with no cognition. An immutable typed
snapshot supplies the same rendered section to initial work, acquisition
follow-ups, effect continuations and later bounded episodes/outcome evaluation.
Activation during an occurrence never hot-swaps its version.

Applicability and preconditions must be considered first. Current Job
responsibility, current operator instructions, runtime authority and fresh evidence
remain controlling. Missing capabilities require an honest limitation; authored
steps cannot manufacture tools, progress, physical effects or delivery authority.
All ordinary acquisition/effect limits and finite episode grammar remain in place.

Compatibility reuses the existing focus **definition identity computation**, not
focus storage or meaning: normalized name, description, target, triggers and
schedule configuration; consumed schedule markers and policy toggles are excluded.
Changed definitions retain old records for inspection but withhold their guidance
and prevent activation. Reject and author a compatible replacement after review.

`disable` is allowed during live/parked work. Projection and nomination recheck
policy and compatibility. Subsequent requests and refreshed provider instructions
withhold guidance without substituting another version. Instructions already sent
to an in-flight provider cannot be recalled. Re-enabling an unchanged definition
can expose only the original captured version to that occurrence; an occurrence
started opted out cannot acquire a procedure midway. Other console mutations for
that Job wait until all its live contexts are terminal. This follows the existing
single-runtime administration convention; concurrent runtime processes sharing a
catalog are unsupported.

## Supplied guidance, reported use, and independent outcomes

Each retained Run record distinguishes its immutable captured version from
`supplied` (the runtime projected it into provider instructions). Merely starting
a Run does not mark guidance supplied. Neither projection nor completion implies
that the model followed it or that it was effective.

Optional feedback has exactly:

```json
{"version":1,"response":"adapted","reason":"One diagnostic was unavailable.","improvement":"Report the missing measurement before interpreting health."}
```

`response` is `followed`, `adapted`, or `declined`; `reason` and `improvement` are
nullable plain text of at most 300 characters each. Declining requires a reason.
Feedback is a cognition self-report attached to the exact supplied Run/version,
not verified execution. Wrong/unsupplied versions are rejected. In v1 feedback is
accepted with completed or failed terminal outcomes, not `continue`, operator stop
or interruption. A later completed Run can separately nominate a revision through
the same operator review process. Feedback does not edit or activate instructions.

SQLite schema **12** adds default-off policy, monotonic version counter, an exact
setup-created experiment marker, and two procedure tables. Migration from schema
11 is transactional; older migrations feed into it. Jobs, IDs, runs, schedules,
activation markers, focus, Findings and retained reports are preserved. No
experimental Job is created at startup or during migration.

Retention is explicit:

- At most two current version records per Job: active and pending. Reject deletes
  pending; replacement deletes the old active record. The version counter never
  reuses an identity. Trial comparisons separately retain their tested snapshots
  and promotion's former method; there is no unbounded review/edit ledger.
- At most **100** opted-in Run evidence records per Job, including live captures.
  On capture, the oldest terminal records outside the window are pruned
  transactionally. Live evidence is never pruned; if live records occupy the
  limit, capture fails closed. Opted-out Runs allocate no procedure record.
- Each Run retains one snapshot/metadata JSON (at most 24,000 characters), supplied
  flag, optional feedback JSON (at most 8,000 characters after escaping), and one
  candidate disposition. No full prompts or payloads are retained. Existing result
  report retention is independent. After pruning, inspection explicitly reports no
  retained procedure evidence rather than inventing a version or use history.
- Disablement retains existing records. Restart retains historical inspection and
  follows existing interruption reconciliation; it does not resume a Run.
  The trial coordinator can allocate new attempts for remaining trial work as
  described below.

## Console commands

Use exact catalog identities and shell-style quoting in the runtime console:

```text
job procedure experiment
job procedure enable JOB<n>
job procedure disable JOB<n>
job procedure show JOB<n>
job procedure activate JOB<n> PROC<version>
job procedure reject JOB<n> PROC<version>
job procedure snapshot RUN<n>
job procedure trials JOB<n>
job procedure trial TRIAL<n>
job procedure seed JOB<n> --name "Name" --purpose "Purpose" --applicability "Conditions" --required-capabilities "Existing tools" --steps "Suggested checks; conditional branches" --verification "How to verify" --stopping-conditions "When to stop or defer" [--source RUN<n>]
```

`seed` authors a pending candidate, never directly activates it. An optional exact
source must be a completed Run belonging to that Job. Without a source Nick may
teach before the first Run. Content is always labeled `operator`, never attributed
to cognition. Pending conflicts require explicit rejection before another seed.
Activation validates exact Job/version, enabled policy and current definition
inside its transaction. Rejection is possible while disabled or incompatible.
No model tool exposes administration or activation.

`show` inspects active/pending provenance and compatibility. `snapshot` inspects
persisted capture, supplied flag, feedback, candidate disposition and independently
accepted Run status, including after restart. These administrative reads remain
available while disabled. Mutations acknowledge persistence only after success;
storage errors do not echo authored text. Experiment creation and manual start
use existing console request provenance (`definition_created` and `run_started`)
with exact identities; procedure source Run provenance remains separate from the
operator interaction's request reference.

## Nick's controlled walkthrough

These commands are an **operator-invoked local pilot**, not changes to a deployed
catalog. From the repository root, use Python 3.13 and create a separate virtual
runtime configuration (`config/experiments/procedure-experiment.toml`) and catalog. The configured provider still needs its normal
credentials and may charge for manual work; automated validation below needs none.

```sh
mkdir -p work
python3.13 main.py --config config/experiments/procedure-experiment.toml
```

The configuration selects virtual hardware, disables actions/messages/earcons and
automatic continuation, and sets `database_path = "../../work/procedure-experiment.sqlite3"`.
That relative path resolves from `config/experiments/` to the isolated catalog under `work/`.
This does not seed, enable or modify the nightly reviewer or any existing Job.
Do not point this pilot at a deployment catalog. The explicit experiment command
allocates a new **Procedure Learning Experiment** Job disabled and unscheduled,
without triggers, with procedure policy enabled for that exact new ID. Its durable
setup marker narrows existing projection to `inspect_self` and no effect tools,
even if application effect permissions are broader. This marker belongs to the
Job setup, not the procedure. No procedure can remove that restriction. The Job
reports current observations, limitations and a useful next measurement, even
without a method. Virtual hardware observations describe only this virtual runtime.

1. Create and inspect. Substitute the returned exact ID for `JOB<n>` everywhere:

   ```text
   job procedure experiment
   job show JOB<n>
   job procedure show JOB<n>
   job enable JOB<n>
   job start JOB<n>
   ```

2. Substitute the allocated first `RUN<n>`; inspect before work (`supplied=false`),
   perform one ordinary bounded episode, and wait for its asynchronous completion:

   ```text
   job procedure snapshot RUN<n>
   job work RUN<n>
   job current
   job result RUN<n>
   job procedure show JOB<n>
   ```

   A first completed Run can propose a method through its existing outcome call.
   Review its report and the pending authored text, source, version and assumptions.
   If it reports `continue`, inspect its limitation; explicitly stop with
   `job stop RUN<n> "Controlled pilot ended"` if appropriate. Do not force completion
   merely to obtain a candidate. No automatic retry or reflection runs.

3. If no candidate was proposed, Nick can teach one explicitly (optional
   `--source RUN<n>` refers to a completed first Run):

   ```text
   job procedure seed JOB<n> --name "Bounded runtime-health triage" --purpose "Produce an evidence-limited health assessment" --applicability "A read-only runtime-health assessment; check current context first" --required-capabilities "Existing inspect_self runtime diagnostics" --steps "Inspect runtime; if a diagnostic is unavailable, record the limitation; otherwise compare current observations" --verification "Separate measurements from interpretation; identify one useful next measurement" --stopping-conditions "Stop after the bounded report; defer unavailable evidence without retry" --source RUN<n>
   job procedure show JOB<n>
   ```

4. Use the exact pending `version` displayed by `show` as `PROC<version>`. Activate
   only after review; it does not run anything:

   ```text
   job procedure activate JOB<n> PROC<version>
   job start JOB<n>
   ```

5. Use the newly allocated **second** Run ID below. Its snapshot automatically
   captures the enabled version; work marks it supplied. Outcome feedback, if any,
   remains a separate model report:

   ```text
   job procedure snapshot RUN<n>
   job work RUN<n>
   job result RUN<n>
   job procedure snapshot RUN<n>
   job procedure show JOB<n>
   ```

6. Evaluate comparable reports: current evidence acquired, limitations recognized,
   next measurement quality, bounded work/cost, applicability reasoning and any
   self-reported adaptation. Keep outcome status separate. For a manual comparison,
   disable procedure policy, start and work a fresh Run, then re-enable while idle.
   Evidence and provider variability limit causal conclusions from a few Runs.

7. Review a later model-proposed revision, or explicitly seed a correction using
   the full command in step 3 with revised text. A pending revision leaves the
   active version unchanged. Inspect it, reject with
   `job procedure reject JOB<n> PROC<version>`, or activate its exact version while
   idle. Inspect the previous Run again to verify its old version remains intact.
   For cleanup, run `job procedure disable JOB<n>` and `job disable JOB<n>`.
   There is no schedule to remove. A separately requested comparison may have
   remaining coordination work and one approval notification, as described below.

## Bounded active/pending comparison

This first comparison contract is only for the **setup-created runtime-health
experiment**, not every Job with procedure policy enabled. `JOB19` and `PROC2`
are reference examples, not stable configuration values: use `jobs`, `job show
JOB<n>` and `job procedure show JOB<n>` against the running catalog to resolve
the actual experiment, incumbent and pending IDs. Startup creates neither an
experiment nor a candidate. This checkout does not include the deployed Jobs
catalog and cannot establish which versions those example IDs currently denote.

When a compatible incumbent and pending candidate exist, the experiment's normal
outcome schema offers nullable `procedure_trial`, with exact integer `active` and
`pending` versions. For example, **only if those are the versions shown by the
catalog**, Mira may submit `{"active":1,"pending":2}`. The existing outcome
request must complete successfully, the exact live binding must remain valid,
and the source Run must accept a completed outcome. Request and source result
commit in the same terminal transaction. Failed providers, cancelled/late
callbacks, opted-out occurrences, other Jobs, mismatched IDs and simultaneous
candidate nominations cannot create a trial. A terminal persistence retry does
not resurrect a lost request. This is not operator dialogue or activation.

Admission snapshots the Job definition identity and text, complete active and
pending records, model/backend/protocol/endpoint and nonsecret provider settings,
profile and startup instructions, timezone, fixed fixture clock, isolation
settings, cases, rubric and budget. Provider credentials are never copied.
There is one campaign per exact pair and definition, including after an
inconclusive result; repeatedly requesting it returns the existing comparison.
At most ten campaigns are admitted per Job, with no automatic pruning. A new
campaign requires a new pending version. These bounds prevent repeated sampling
until a candidate happens to win.

The background coordinator offers **one attempt at a time**, at the existing
configured Job scheduler polling interval. This coordination is independent of
`jobs.auto_continue`: it starts new isolated attempts, never revives a Task or
continues an interrupted Run. The budget is **14 attempts**, **one ordinary
bounded Job episode per attempt**, and **60 seconds per attempt**, including
startup/provider work. Six cases require twelve attempts; the remaining two
opportunities cover interruptions. Every replacement consumes another attempt
and receives a new catalog Run ID. Provider errors or noncompletion produce a
failed scored attempt; they are not retried for a better answer.

Each attempt uses a fresh virtual `RobotApplication` and private in-memory
catalog, the pinned original Job and variant, fixed platform observations and
diagnostic responses, and isolated historical working memory. The private
catalog preserves the canonical Job/Run IDs. The normal Task/Job executor and
procedure projection supply the selected variant; setting the candidate active
inside this disposable catalog never changes production selection. The supported
OpenAI adapters use independent request/observability state with their ordinary
HTTP client; custom backends must explicitly implement a trial fork. This is a
runtime capability boundary, not an OS sandbox for arbitrary executable code.

This avoids toggling application-wide effect policy and avoids borrowing the
production Job's single live occurrence. The experiment capability restriction
still projects only `inspect_self`; the private application has no production
Workspace, persistent memory, delivery sink, camera, body, earcon output, shared
event bus or resources. All effect calls and procedure nominations/recursive
trial requests are rejected at execution. Candidate feedback remains a separate
exact-version self-report. Ordinary manual, scheduled and triggered occurrences
continue to capture the incumbent and retain their single-live-occurrence rule;
isolated trial Run rows do not participate in their admission, latest-completed
result selection or ordinary daily Job activity totals. `job runs` includes trial
IDs; the comparison identifies their
case, version and disposition.

The catalog persists an attempt before provider work, then imports its terminal
result, projection evidence, bounded tool trace, scores, provider calls and cost
accounting atomically. A crash between execution and import leaves an interrupted
attempt, not fabricated completion. Startup's ordinary reconciliation interrupts
orphaned Runs. Coordination preserves completed attempts and allocates a new Run
only for remaining work within budget. A changed Job, either changed snapshot,
model/configuration or trial contract stops remaining coordination as stale.
Completed comparisons and their reports remain inspectable after restart.

### Fixed evaluation and evidence limits

`runtime-health-v1` fixes these cases before work:

| Case | Independent expected behavior |
| --- | --- |
| Normal runtime diagnostics | Describe bounded normal diagnostics; leave physical health unverified |
| Missing diagnostics | Preserve unknowns and request a runtime diagnostic sample |
| Stale normal history, current memory pressure | Use the acquired current pressure observation and suggest a memory sample |
| Physical calibration request outside runtime-health scope | Decline procedure applicability and request scope clarification |
| Evaluation-only recovery | Prefer current normal diagnostics over old pressure claims |
| Evaluation-only missing diagnostics | Preserve unknowns despite an old overall-health claim |

The last two cases are held out from the runtime's nomination/development context;
their inputs first enter cognition in evaluation attempts. They are fixed fixtures
in source, not a secret or statistically independent population after repeated
operator examination. Do not tune candidates against their reported answers and
then describe subsequent evidence as held out.

Both variants receive identical case input values, history, instructions, clock,
platform and diagnostic tool behavior. Their procedure, Run and Task identities
necessarily differ. The trial asks for a structured JSON **result report** with
assessment, exact acquired evidence, limitation, next diagnostic and applicability.
It scores that work artifact against the pinned case oracle and independently
recorded acquisition results, not feedback's claims of following the method.
Free-text summaries and intermediate model commentary are supporting context,
not independently graded health reports. Malformed/absent structured work or
unacquired citations are inconclusive; this intentionally narrow format is not a
general semantic evaluator of arbitrary health prose.

Current-evidence use scores 0–2, useful next diagnostic 0–2, and applicability 0–1.
For an outside-scope request the authoritative case scope can justify declining
without an irrelevant diagnostic acquisition; empty evidence is valid there.
The next diagnostic has a fixed case-specific best choice and useful alternative;
other recognized suggestions earn zero. Every candidate case **and criterion**
must avoid regression, all independent evidence must be valid, and the candidate
must gain at least two total points. Unsupported health conclusions, stale claims
presented as current, missing protected limitations and incorrect applicability
block promotion. Invalid or incomplete evidence yields **inconclusive**; equal or
insufficient improvement retains the **incumbent**. No model judge or reported
procedure-following score can override these gates. Calls, provider-reported
usage and reported/estimated cost are inspectable when supplied; missing cost
remains unavailable, not zero. Provider variability, finite case coverage and the
constrained report format limit these results. A win demonstrates this synthetic
contract, not actual deployed health, general procedure effectiveness or causal
improvement.

### Operator review, approval and rollback

1. Resolve the actual Job and procedure IDs in the running catalog. Review or
   develop the pending method through the existing nomination/seed workflow;
   leave the incumbent active. Work a fresh ordinary experiment Run so Mira can
   request the bounded comparison. A trial is optional, not automatically created
   merely because a pending method exists.
2. Inspect `job procedure trials JOB<n>` and `job procedure trial TRIAL<n>`.
   The latter shows recommendation/reason, score totals and regressions, every
   attempt's Run ID, both exact snapshots, fixed inputs/rubric/budget, tool
   evidence, feedback and cost. `job result RUN<n>` and `job procedure snapshot
   RUN<n>` also inspect retained attempt evidence. The full comparison retains
   evidence even beyond the ordinary 100-snapshot window.
3. A winner can request approval through the existing configured autonomous
   notification route (`initiative.messages` plus an eligible operator sink).
   The message references `TRIAL<n>` and prints the exact command:

   ```text
   job procedure activate JOB<n> PROC<tested-candidate-version>
   ```

   No route means `not_configured`; delivery failure means `rejected`. Neither
   removes the report or changes the incumbent. Notification intent is durably
   claimed **before** delivery, so ordinary restart never repeats that attempt.
   A crash can therefore leave `claimed` without confirmed delivery. There is no
   receipt, acknowledgement, reply binding or automatic notification retry.
4. Review the report, then use that exact console activation command while the
   Job is idle. Activation for a compared candidate requires a winning, still
   current recommendation, exact incumbent and candidate snapshots, compatible
   Job definition and enabled Job/procedure policy, checked transactionally with
   promotion. An old recommendation cannot approve a replaced method. Compared
   losing/inconclusive versions cannot bypass the gate by using manual activation;
   reject and revise them. Manual activation for methods outside comparisons
   remains available. Activation itself does not start work.
5. A fresh ordinary Run now captures the winning version under the same Job
   definition. The promotion record in `job procedure trial TRIAL<n>` retains the
   former active method's **exact text and provenance**, the tested candidate,
   definition and approval time. To restore the previous method, reject any
   pending replacement, use the existing full `job procedure seed JOB<n> ...`
   command with all seven fields from `promotion.previous.instructions`, inspect
   the new pending version, and explicitly activate that version while idle.
   This restores the exact method text as a new operator-authored version, with
   the original version/provenance recoverable in the promotion record. It does
   not reuse a version ID or rewrite earlier Run snapshots. A changed definition
   requires a fresh compatibility review before restoring old instructions.

## Automated validation

```sh
python3.13 -m unittest tests.test_job_procedures -q
python3.13 -m unittest tests.test_procedure_trials -q
python3.13 -m unittest discover -q
python3.13 -m compileall -q embodied_runtime tests
git diff --check
```

Fake providers, virtual backends and temporary SQLite databases exercise the full
cycle and failure paths without physical hardware or live providers. This validates
mechanism and boundaries, not learned-method quality or effectiveness.

Comparison-extension validation: 70 focused tests passed, including 50
procedure/trial tests and the daily-activity tests. A broader 221-test Job and
benchmark regression selection also passed. The full suite after rebase ran
1,872 tests with 18 skips and two failures in the existing compatible-provider
fixture for `power_recovery_continuity` (parallel-tool modes `disable` and `omit`).
Both failures reproduce on untouched `main` at `711decf`; they are independent
of procedure trials. Compileall and diff whitespace checks passed. Coverage includes paired inputs,
ordinary/scheduled incumbent selection, forbidden capabilities/nomination,
protected regressions, inconclusive evidence, bounded replacement attempts,
shutdown/restart, atomic persistence, stale approval, notification failure and
deduplication, promotion and rollback. Live-provider quality, deployed catalog
identities and an operator-observed notification/activation walkthrough remain
unvalidated; no deployed Job or physical hardware was changed.

Historical validation of the original nomination slice in its managed
development environment: 29 procedure tests and 203
focused regression tests passed with the standard runner. The full suite ran
1,513 tests with 16 skips and no failures using a temporary environment-only
runner. The ordinary full-suite command hung in existing asyncio executor shutdown;
a repository-independent `asyncio.run(asyncio.to_thread(lambda: None))` also hung.
The temporary runner capped selector blocking at 50 ms without changing production
code or mocking executor work. It also skipped one unchanged Workspace integration
test because the sandbox prohibits Unix-socket bind; the other 15 skips were normal.
Compileall, diff whitespace checks and strict loading of the pilot configuration
passed. Use the ordinary full-suite command on a normal unrestricted host.
