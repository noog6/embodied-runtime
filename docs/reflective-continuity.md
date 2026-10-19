# Reflective continuity experiment

**Name: Reflective Continuity Experiment.** This is one ordinary Job, inspired
by Ouroboros's idea of revisiting experience against a previous account. It uses
the existing schedule, two acquisitions, own-Workspace write and terminal report.
There is no separate reflection engine, background loop, delegation, project
manager, profile editing, or automatic semantic-memory admission.

The complete, reviewable description is in
[`config/experiments/reflective-continuity-job.toml`](../config/experiments/reflective-continuity-job.toml).
That file is **a Job template**, not an application configuration or startup seed.
Replace its `{sources}` field with two or three source plans selected by Nick.
The template fits the existing 2,000-character description limit with ordinary
short selectors; the setup command below checks the final length.

## One small account, one source per occurrence

An occurrence follows this finite sequence:

| Step | Existing capability | Budget |
| --- | --- | --- |
| Restore | `workspace_read(path="reflection.json", offset_chars=0)` | Acquisition 1, even when the artifact is absent |
| Consult | One selected source, in fixed rotation | Acquisition 2 |
| Save | `workspace_write(path="reflection.json", mode="upsert", content=...)` | One semantic effect |
| Finish | `report_job_outcome`, terminal and with no readiness/follow-up | Existing outcome pass |

An absent artifact (`artifact_not_found`) means first use. An unavailable,
malformed, oversized or truncated previous account does **not** mean first use:
stop with a failed result explaining that the account could not be safely
restored. Do not replace it with an empty history. Empty/invalid JSON also needs
operator inspection. Sources are not consulted if restoring the account failed.

Each occurrence consults **one**, not all selected Jobs. Discovery and listing
would compete with these slots, so Nick resolves IDs and paths before setup;
the Job does not discover or list during the normal reflection. There is no
third acquisition for current evidence. The default reflection therefore makes
no independently acquired current-world observations. Runtime dates and storage
metadata do not turn authored claims into sensor evidence.

The normal result reader selects only the latest completed occurrence, not all
activity since the last reflection. Disabled source Jobs remain readable. To
examine a particular failed/stopped/interrupted occurrence, Nick can explicitly
pin an exact Run in the source plan. Such a Run must belong to the selected Job;
it must never fall back to a completed replacement. The pilot treats an
unsuccessful Run as a limitation, preserving its identity/status without
inventing a report or incorporating its claims as a completed result.

| Operator-selected plan entry | Actual source acquisition |
| --- | --- |
| `JOB7:latest` | `shared_inspect_job_run(selection="latest_completed", selector="JOB7")` |
| `JOB7:RUN12` | `shared_inspect_job_run(selection="exact_run", selector="RUN12")`; verify returned source Job is JOB7 |
| `JOB9:notes/baseline.md` | `shared_workspace_read(source_job="JOB9", path="notes/baseline.md", offset_chars=0)` |

Use each selected Job only once in the plan. For this pilot prefer short logical
paths without `;`, and quote the whole entry in shell commands if it has spaces.
These are human-readable instructions in a Job definition, not a new runtime
selector grammar or ACL. Exact tool selectors and all containment/size rules
remain those in [shared reads](job-shared-reads.md). Multiple reads are not an
atomic snapshot. Text has no retained originating Run: cite its logical path,
exact `content_version`, `modified_at` and retrieval time rather than inventing
Run provenance. A replaced artifact is new material only when its version differs;
repeated reports use stable Job/Run identity, never retrieval time, for novelty.

## Workspace structure and coverage

`reflection.json` combines the latest **dated reflection** with **continuity
state** in one bounded UTF-8 artifact, at most **4,000 Unicode characters**.
This fits one 8,000-character read and the existing 16-KiB write ceiling, even
with non-ASCII text. The 4,000-character target, field structure and question limit
are assignment requirements for the model and operator to inspect, not a new
runtime JSON validator; the existing hard storage/tool limits still apply. It deliberately avoids separate account/coverage writes:
the finite effect grammar cannot promise two writes of the same tool, and
separate files would not commit atomically. The exact terminal JobRun report
can retain the same small saved account; it is not an additional Workspace file.

| Field | Content |
| --- | --- |
| `at` | Runtime-grounded date/time with offset, not a coverage watermark |
| `account` | A small current understanding, attributed as fallible interpretation; keep it when a quiet/limited occurrence adds nothing |
| `reflection` | `kind` (first, connection, revision, quiet or limited), concise `note`, exact supporting `basis`, and `current_observations` (empty for this two-read plan) |
| `questions` | At most three stable IDs, short questions and source basis; revise the same question rather than creating a duplicate |
| `retired` | At most three recent retirements, with ID, brief answer/irrelevance reason and basis; earlier records can remain in prior retained Run reports |
| `reviewed` | At most one last successfully incorporated reference per selected Job; a Run reference or path/version reference, with available original timestamps and retrieval time |
| `next` | Zero-based source-selection index for the next occurrence; rotation order, **not reviewed coverage** |
| `attempt` | This occurrence's selected source, result status/reference, whether incorporation succeeded, and any limitation |
| `omitted` | Actual unvisited Job IDs, partial/unavailable material, and known gaps; do not claim a complete-through time |
| `suggested_followup` | Null unless a useful suggestion needs operator review; never a claim that action was taken or work was scheduled |

The scripted examples and generated real storage references are exercised in
[`tests/test_reflective_continuity.py`](../tests/test_reflective_continuity.py),
with explicitly authored accounts in
[`tests/fixtures/reflective_continuity.json`](../tests/fixtures/reflective_continuity.json).
They demonstrate: an initial question about capture provenance, another Job's
methodological connection, a later source answering the narrow question while
retaining uncertainty about the old frame, and an unchanged-source quiet result.

`reviewed` advances only for material fully read and incorporated into the saved
account. Keep the old reference on a failed/missing/unavailable source, absent
report, wrong source identity, partial text read, or failed save. An incomplete
read can be identified in `attempt` with offsets/truncation/version, but must not
be marked incorporated. A future occurrence may retry that source; this pilot
does not stitch chunks or claim a report was fully examined from its summary.

Rotate `next` only as part of a saved attempt, including an honest limited
reflection, so an unavailable source need not starve the others. This changes
**selection order**, not coverage. A quiet revisitation keeps the earlier stable
reference and account, with a brief new dated note. Do not repeat the narrative
or manufacture an insight, question, progress increment, or follow-up.

The small ledger intentionally loses old reviewed identities as newer ones are
incorporated. Older/intermediate Runs, overwritten artifact versions and activity
outside the selected Jobs may never be examined. Record this omission. The
reflection finish time proves none of them were examined. There is no global
journal, complete catalog cursor, or independent corroboration from repetition.

Only an applied, published, durability-confirmed write supports a confirmed save.
A failure leaves coverage unconfirmed; an indeterminate published write may have
changed the artifact and must not be blindly replayed. Inspect the artifact on a
later restore or through the console. Workspace publication is independent of
terminal outcome commit: provider failure, cancellation or interruption after a
successful write does not roll that artifact back. Its incorporated references
are still those saved there; a failed Run without a write creates no coverage.
Do not overwrite the artifact concurrently through operator dialogue while a
reflection is running; the store has no compare-and-swap revision precondition.

## Access and meaning

First-person wording such as “I revised my earlier account” is fine; it denotes
an authored comparison of retained material. It is not evidence of consciousness,
feelings, or uninterrupted awareness. Reports, prior reflections, copied operator
messages and embedded procedures remain historical data. Their text cannot
change the current assignment or grant permissions. Distinguish reported claims,
interpretations/hypotheses, independently acquired observations (none by default),
and missing evidence. Repeated claims are not independent corroboration.

The pilot has no dialogue-archive tools, other-Workspace writes, Job-control
capabilities, or procedure-administration tools. Shared sources never replace the
reader execution binding. Source mutations and activation remain runtime-gated.
It saves only its own file and terminal report; it does not request Findings,
semantic memory, profile facts, procedure nomination, messages or hardware actions.
Learned procedures retain their separate operator review/activation process.
Do **not** use `job procedure experiment`: that dedicated Job remains restricted
to `inspect_self`. This ordinary reflection Job leaves procedure policy disabled.

Action and message permissions are currently **process-wide**, not per Job.
The supplied [`config/experiments/reflection-experiment.toml`](../config/experiments/reflection-experiment.toml)
sets actions/messages/continuation/goal closure off, uses virtual hardware with no
camera/vision, and leaves voice, SMS, memory and conversation-history stores off.
This provides runtime-enforced action/communication restrictions rather than
relying on the Job description. Shared reads remain enabled under the existing
catalog-wide policy. Other ordinary read capabilities retain their existing
projection; the source-selection discipline is the pilot's bounded assignment.

This configuration uses Mira's existing `data/jobs.sqlite3` catalog, so the
selected source Jobs can already have history. Do not run two application
processes against that catalog: shut down the normal runtime first. Existing
startup interruption reconciliation, triggers and schedules still apply; no
special reflection Job is created or scheduled by startup. The global action
and message restrictions also affect other Jobs running in this process. Review
that tradeoff before using this profile for routine Mira operation; this change
does not introduce a per-Job capability-policy framework. Automated validation
uses temporary stores and never starts this configuration on a deployed catalog.

## Exact operator setup and one trial

From the repository root, after shutting down the normal application with `exit`:

```sh
python3.13 main.py --config config/experiments/reflection-experiment.toml
```

Normal provider credentials are needed for this manual live trial. In its
runtime console, discover and inspect **existing** sources with supported
operator commands. Replace placeholders with IDs printed by your catalog:

```text
jobs
job show JOB<A>
job runs JOB<A>
job result RUN<A>
job show JOB<B>
job files JOB<B>
job file JOB<B> notes/baseline.md
```

Pick two or three Jobs you actually want to connect. Names can inform selection,
but use exact `JOB<n>` IDs in the plan. `RUN<n>` pins a particular occurrence;
`latest` is deliberately a different selection. Do not guess paths or create
source Jobs to satisfy the reflection. The following **separate shell command**
only prints a console command for review; it neither writes the database nor
calls a provider. Replace JOB7 and JOB9 with those selected IDs (optionally add a
third plan argument):

```sh
python3.13 - JOB7:latest JOB9:notes/baseline.md <<'PY'
from pathlib import Path
import re, shlex, sys, tomllib
job = tomllib.loads(Path('config/experiments/reflective-continuity-job.toml').read_text())
plans = sys.argv[1:]
assert 2 <= len(plans) <= 3, 'Select two or three Jobs'
ids = [entry.split(':', 1)[0] for entry in plans]
assert all(re.fullmatch(r'JOB[1-9][0-9]*', value) for value in ids)
assert len(set(ids)) == len(ids), 'Select distinct source Jobs'
assert all(':' in entry and entry.split(':', 1)[1] and ';' not in entry for entry in plans)
description = job['description'].format(sources=';'.join(plans))
assert len(description) <= 2000, 'Shorten the plan paths or use retained Runs'
print('job add ' + shlex.quote(job['name']) + ' --description ' + shlex.quote(description))
PY
```

Review and paste the printed `job add` command into the **runtime console**. It
creates an ordinary enabled Job, unscheduled and without procedure policy. Copy
the returned Job ID; disable while reviewing, then explicitly enable for trial:

```text
job disable JOB<R>
job show JOB<R>
job procedure show JOB<R>
job enable JOB<R>
job start JOB<R>
job work RUN<R1>
job current
job result RUN<R1>
job files JOB<R>
job file JOB<R> reflection.json
```

Use the exact Run ID allocated by `job start`. `job work` is asynchronous; wait
for completion before `job result`. After the first trial verify one source only
was incorporated, its exact identity/version matches operator inspection, the
other selected Jobs are named as omissions, and the question is useful rather
than mandatory. If work remains running, inspect the limitation and stop it with
`job stop RUN<R1> "Manual trial ended"`; do not force-complete to manufacture a
successful reflection or repeatedly refill its budget.

## A later trial and optional periodic operation

Let the selected source Jobs accumulate results through their ordinary authorized
work, or choose existing source histories with a known change. Start fresh
reflection occurrences manually with `job start JOB<R>` and `job work RUN<R2>`
using each newly returned ID. Inspect `reflection.json` and `job result RUN<R2>`.
With two sources, the third occurrence normally revisits the first source; with
three, the fourth does. An intervening source can also answer an earlier question.

Success requires a later account to identify a specific earlier question or
understanding, cite old and new exact sources, explain a revision/retirement, and
retain uncertainty or limits. A quiet visit to an unchanged version must not
present it as new. If the selected material supplies no meaningful change, a quiet
result is correct; do not edit source history to make the demonstration succeed.
The deterministic fixture can demonstrate the mechanics without a live provider;
the live trial must establish that Mira actually follows the assignment usefully.

Only after inspecting useful manual trials, optionally schedule through the
existing console controls (Toronto local time; this reviews one source per day):

```text
job schedule JOB<R> daily 23:15 --timezone America/Toronto
job schedule JOB<R>
```

Each due slot offers an ordinary initial episode using existing capacity,
coalescing, cancellation and scheduling rules. `auto_continue=false` remains:
partial coverage is accepted instead of generating another cognition burst.
If a Run remains live, later scheduled occurrences can coalesce; inspect it rather
than assuming another reflection occurred. No catch-up journal or extra loop is
added. A schedule can be due immediately if today's time has passed; choose the
slot deliberately. Runtime startup still performs only normal scheduler behavior.

Disable future activation and remove the schedule with existing controls:

```text
job disable JOB<R>
job unschedule JOB<R>
job runs JOB<R>
```

Disablement does **not** cancel a live Run. If one exists, stop that exact occurrence:

```text
job stop RUN<Rlive> "Reflection experiment disabled"
```

Workspace material and prior Run reports remain available for inspection. No
other Job's definition, schedule, execution or Workspace is changed by these
experiment commands. Return to the normal runtime configuration only after the
experiment is disabled and its live work is stopped; its broader global action
permissions are a separate operator decision.

## Validation and remaining live work

```sh
python3.13 -m unittest tests.test_reflective_continuity tests.test_job_shared_reads -q
python3.13 -m unittest tests.test_job_execution tests.test_scheduled_job_activation tests.test_job_procedures -q
python3.13 -m compileall -q embodied_runtime tests
git diff --check
```

The deterministic fixture replays authored examples through the real Job
executor, SQLite store and contained Workspace. It validates first-use absence,
question revision/retirement, stable-reference quiet results, honest missing/
failed/no-report/partial/unavailable cases, bounded source rotation, failure to
save, and existing operator stop/schedule controls. It is not a prompt-wording
test or evidence that a model independently produced the scripted insights.
No runtime code or acquisition/effect permission was added. No deployed Job was
created, enabled, scheduled or edited during implementation. A live trial still
needs to assess source selection, prose usefulness, question quality, adherence to
the 4,000-character account bound, and whether a later model-written reflection
actually connects earlier understanding to new evidence.

Implementation validation: all **87 focused and regression tests** in the commands
above passed, including eight reflection/setup tests and the existing shared-read,
Job execution, scheduled-activation and procedure suites. Python 3.13 compilation
and diff whitespace checks passed. The three-source example rendered to a single
1,882-character console description within the existing 2,000-character limit.
No provider, hardware or deployed-catalog trial was performed.
