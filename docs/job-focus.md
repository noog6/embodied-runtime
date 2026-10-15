# Carry-forward Job focus

Carry-forward is opt-in task continuity: one bounded unresolved question can pass
from a completed occurrence to later occurrences of the **same Job**. Every Job,
including existing deployed Jobs, starts with `carry_forward_enabled=false`.
Enabling it changes neither the description nor activation policy. JOB4 and the
nightly reviewer are outside this experiment; do not enable or seed them.

## Record and limits

The Jobs SQLite database retains at most one current `JobFocus` per Job:

| Field | Limit and meaning |
| --- | --- |
| `question` | One unresolved question; 1–500 Unicode characters |
| `status` | `open` or `blocked` |
| `blocker` | Optional 1–300 character explanation; only with `blocked` |
| Clear `reason` | Required 1–300 character resolution/irrelevance explanation, validated but not retained as a ledger |
| `basis` | Optional one of `wake_event`, `acquisition_1`, `acquisition_2`, `effect_1`, `effect_2` |
| Runtime provenance | Job ID, exact completed source Run ID, author (`model` or `operator`), Task UUID (at most 100 characters) and episode ID for model nominations |
| Timestamps | UTC source Run finish time, focus creation time, update time |
| Version | Positive per-Job version, advanced on set/clear; keep advances nothing |
| Definition identity | 64-character SHA-256 of normalized name, description, target, trigger configuration and daily schedule configuration |

Authored text must be nonempty plain text without Unicode control/format/surrogate
characters. Limits apply **before** surrounding whitespace is trimmed; oversized
values are rejected, never silently truncated. There is no semantic classifier to
prove that a string contains exactly one question. Cognition and operators should
nominate one unresolved focus, not pack several questions into a list.

Only bounded text and metadata are stored. Focus does not retain full prompts,
transcripts, acquisition payloads, images, or a revision history. Replacing a focus
overwrites the single record; clearing deletes it. The per-Job version counter
survives deletion so later records do not reuse an earlier version. The immutable
JobRun result remains an independent historical work product, not a focus ledger.

## Nomination and commit

`report_job_outcome` gains nullable `focus_update`. No separate cognition tool,
provider request, acquisition, effect, or loop is introduced. Older callers may
omit the field. Null means keep, including when the feature is disabled.

```json
{"action":"keep","question":null,"status":null,"blocker":null,"reason":null,"basis":null}
```

```json
{"action":"set","question":"What evidence remains inaccessible?","status":"blocked","blocker":"Existing tools expose no handset confirmation.","reason":null,"basis":null}
```

```json
{"action":"clear","question":null,"status":null,"blocker":null,"reason":"No longer applicable to this responsibility.","basis":null}
```

Every object must contain exactly these six fields. Set requires a question and
status, null reason, and an optional blocker for blocked status. Keep requires all
other fields null. Clear requires a reason and null question/status/blocker. Set
and clear may reference one available accepted runtime evidence basis from this
episode. Missing, rejected, historical Workspace/image acquisition references are
not accepted as current evidence bases. A reference proves only that the named
runtime evidence existed; the interpretation that it resolves a question remains
fallible. Neither clear nor set requires a reference when there is none to cite.

For v1, set/clear requires an accepted **completed** Job outcome and carry-forward
enabled at occurrence start and commit. Continue and failed outcomes must use
null/keep; within-occurrence work continues to use existing progress and readiness.
A terminal Run can legitimately finish its bounded responsibility and leave an
unresolved question for a future occurrence; focus does not weaken the existing
TaskGoal completion requirements.

The harness derives ownership from the exact live Job/Run/Task/ActiveGoal binding.
The model cannot supply a Job, Run, Task, source, timestamp, author, or version.
Proposals are staged only in the outcome request closure. After the complete
provider response succeeds, the harness rechecks binding, cancellation, feature
policy, durable source Run, and Job-definition identity. SQLite revalidates policy,
definition identity and Run ownership inside the terminal transaction. The completed
Run result and focus update commit together under `BEGIN IMMEDIATE`.

Provider failure after a tool callback, cancellation, late callbacks, invalid
proposals, stopped/interrupted work and stale bindings publish no proposed focus.
Late outcome callbacks are closed even if the provider returned without a proposal.
SQL statement or commit failure rolls back both result and focus. Task
terminalization follows the existing runtime lifecycle before persistence; a failed
terminal write can leave its exact Task terminal for explicit result retry. Such a
retry does not resurrect a lost nomination. No speculative focus is published and
there is no automatic persistence retry. Runtime logs record only bounded IDs,
actions/status, versions and lengths, never focus prose or evidence payloads.

## Projection and authority

Occurrence start selects the enabled, applicable current focus and captures an
immutable typed record and rendered snapshot. The snapshot is a separate context
section in initial work, acquisition follow-ups, effect continuation and outcome
cognition, consistently reused across that occurrence's bounded episodes. It includes
source Run, author, original timestamps, age at snapshot, status and blocker. Console
inspection includes the same provenance. Runtime metadata in the JSON is authoritative
for its source identity; authored question/blocker prose is historical context.

Keeping a focus, reading it again, or starting another occurrence never refreshes
its source or evidence age. An operator seed has a new authoring timestamp and retains
the old source Run finish timestamp separately. Unknown historical evidence age is
not inferred from when the operator seeded it. An operator seed's source Run does
**not** prove that the seed text appeared in the report or that the report's claims
are true. An operator explicitly asserts applicability to the current Job definition;
legacy reports do not have a recoverable original definition fingerprint.

Focus is not verified world state, current evidence, or an overriding instruction.
The common harness guidance says to consider it when selecting evidence, while
current responsibilities, urgent conditions and authoritative evidence take
precedence. Use only offered tools and permissions. Identify inaccessible evidence;
do not pretend to inspect it, and do not repeat effects merely to recreate evidence.
Keeping or clearing a question grants no external effect, image-save or delivery
authority. Blocked focus itself never schedules, polls, notifies, reserves resources,
or creates a Run. It creates no readiness condition and performs no activation.

Name/description/target, trigger configuration or daily schedule configuration
changes make a stored focus incompatible: it is retained for operator inspection
but withheld from cognition. The schedule's consumed date marker, enablement of the
Job itself, and focus policy are not responsibility content in the fingerprint.
Projection also checks current focus policy and definition, so an out-of-band change
during a live occurrence withholds its old snapshot. It never substitutes a newly
edited focus into that occurrence. Identical normalized definitions are compatible;
this is a content identity, not a revision ledger. Explicitly replace or clear stale
focus after reviewing the changed responsibility. Disabling withholds nomination
and projection but retains the record; re-enabling with an identical definition
makes that record applicable again. No focus/policy means ordinary work behavior.

| Other mechanism | Distinction |
| --- | --- |
| JobProgress | Runtime-earned counters for one live occurrence; no cross-occurrence recovery |
| Semantic continuity | Latest bounded summary between episodes of one occurrence; volatile |
| Readiness | Existing authority for continuing the same Run; focus has no wake semantics |
| JobRun results | Immutable terminal reports per Run, requiring explicit retrieval when wanted |
| Findings | Historical claims published/searchable through existing bounded tools; focus is one private question automatically projected for its own Job |
| Semantic memory | Separate admission/recall policy; no focus admission or learning |
| Workspace handoffs | Durable Job-owned artifacts read through acquisition; focus is supplied without Workspace access |

This feature does not make SMS delivery history or handset confirmation accessible
when existing tools do not expose them. It adds no delivery capabilities, cross-Job
Workspace access, procedural learning/guides, reviewer automation, automatic planning,
scheduling, code changes or expanded action authority.

## Console grammar

Use the existing exact `JOB<n>` and `RUN<n>` catalog identities; names, prefixes and
fuzzy matches are rejected. Quotes follow the console's existing shell-style parsing.

```text
job focus enable JOB<n>
job focus disable JOB<n>
job focus show JOB<n>
job focus snapshot RUN<n>
job focus seed JOB<n> RUN<n> --question "One question?" [--status open|blocked] [--blocker "Explanation"]
job focus clear JOB<n> --reason "Resolved or irrelevant"
```

Enable/disable only persists policy. Seed explicitly replaces the current record,
requires policy enabled and an exact **completed** source Run belonging to that
Job, and always records `author=operator` with no model Task/episode identity.
Seed defaults to open. Clear is allowed even while disabled. `show` displays current
provenance and applicability; its age is evaluated at inspection time. `snapshot`
displays the fixed occurrence snapshot before work or while the Run remains live;
there is no retained full prompt to inspect after terminal cleanup.

All focus mutations and description updates reject conflicting administration while
**any** live context for that same Job exists, including parked contexts and contexts
behind another foreground Job. Different Jobs remain independent. This is a
single-runtime console guard, not distributed administration locking: do not run
multiple processes against the same execution catalog. Commands acknowledge
`persisted` only after store success; persistence errors do not echo payloads or
claim success. Read-only show/snapshot remain available during work.

## JOB15 post-merge pilot

These are operator commands to run after merging; they were not executed against a
deployment during development. Leave JOB4 and the nightly reviewer disabled for
carry-forward. Do not edit any deployed descriptions, triggers or schedules.

Use one runtime process for the catalog. For a bounded manual pilot with no message
permission, automatic continuation or hardware effects, create a separate local
configuration from the repository root (preserving the deployment configuration):

```sh
mkdir -p work
cat > work/job15-focus-pilot.toml <<'TOML'
[runtime]
profile = "mira"
hardware = "virtual"
camera = "none"
cognition = "openai-responses"
vision = "none"
mode = "console"
timezone = "America/Toronto"

[cognition]
model = "gpt-6.1-sol"

[initiative]
enabled = true
actions = false
messages = false
continuation = false
goal_closure = false

[earcons]
enabled = false

[jobs]
enabled = true
database_path = "../data/jobs.sqlite3"
auto_continue = false
TOML
python main.py --config work/job15-focus-pilot.toml
```

This uses the existing catalog and configured provider credentials. Running Job
cognition here uses the configured provider and may incur its normal charge. SMS,
voice, and message effects are disabled in this standalone configuration. Existing
catalog triggers/schedules still retain their normal activation rules; run the pilot
in an appropriate window and inspect live Runs. Focus introduces no activation.

First inspect the exact source and enable only JOB15 when it has no live Run:

```text
job show JOB15
job result RUN391
job focus enable JOB15
job focus seed JOB15 RUN391 --question "What evidence can existing tools provide about whether the SMS discussed in RUN391 reached the handset?"
job focus show JOB15
job start JOB15
```

Confirm RUN391 is completed and belongs to JOB15; otherwise seed fails and the
operator must resolve the source mismatch. The question above is an operator
formulation based on the report, not an asserted quotation or handset observation.
`job start` prints the newly allocated `RUN<n>`; substitute **that exact ID** below:

```text
job focus snapshot RUN<n>
job work RUN<n>
job result RUN<n>
job focus show JOB15
```

Snapshot must show RUN391, `author=operator`, original source finish time, open status
and the bounded question. If the Job completes and proposes null/keep, show must
retain the original version/source/timestamps. If it chooses a blocked replacement,
show identifies the completed pilot Run as its new model source and its blocker.
If work is nonterminal, `job result` truthfully has no terminal report; `job current`
shows existing readiness and the focus is unchanged. Auto-continuation is disabled;
use `job stop RUN<n> "Manual pilot ended; no automatic retry"` if ending that live
occurrence. Do not force successful completion merely to publish a focus.

To explicitly demonstrate blocked historical context after that Run is terminal,
use the same exact completed historical source:

```text
job focus seed JOB15 RUN391 --question "Can existing tools establish handset receipt of the SMS discussed in RUN391?" --status blocked --blocker "SMS delivery history and handset confirmation are not exposed by the currently offered tools."
job focus show JOB15
```

This changes only the bounded focus record. It sends no message and creates no
retry, timer or Run. A model can similarly nominate blocked in a completed outcome
using the JSON example above, or retain the question with null/keep. No seed or
model interpretation makes missing delivery evidence accessible. Optional cleanup:

```text
job focus clear JOB15 --reason "Pilot ended; remove this experimental question."
job focus disable JOB15
```

## Local validation

No paid providers or physical hardware are needed:

```sh
python -m unittest tests.test_job_focus -q
python -m unittest discover -q
python -m compileall -q embodied_runtime tests
git diff --check
```
