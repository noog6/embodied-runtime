# OpenAI text cognition setup

OpenAI Responses support is optional; the ordinary runtime needs neither the
SDK nor an API key. Install the project with only this optional integration:

```sh
python -m pip install -e '.[openai]'
```

`OPENAI_API_KEY` remains the runtime's application-facing authentication
interface. Do not commit it or put it in source or command-line arguments. See
[Secrets and API keys](secrets.md) for the recommended Raspberry Pi and local
development storage and delivery procedure. The default experimental model is
`gpt-5.6-luna`; `OPENAI_MODEL` may override it. Then start the local console:

```sh
python main.py --cognition openai-responses --console
```

Add `--initiative` for bounded goal-directed initiative. It may provide one
read-only `inspect_self`, `observe_scene`, `recall_memory`, or
`inspect_run_history` acquisition where available, plus
the single bounded `schedule_followup` semantic effect. Historically initiative
used exactly `tools=()`; that description is no longer current.
`--initiative-actions` independently offers `orient_body` on a nonphysical body.
`--initiative-messages --console` offers the transport-neutral
`address_operator(message)` capability, with the console as today's concrete
operator channel. A statement or question is delivered once and never waits for
a reply. With both permissions the adapter may receive both definitions, while
`RobotApplication` independently enforces one capability request total.

`--initiative-goal-closure` requires only initiative; scheduling supplies the
base policy's semantic effect without action or message permission. One actually
requested effect receives one independent outcome evaluation
using fresh Runtime context, the same captured ActiveGoal and WorkingMemory
snapshot, the original stimulus, and the runtime-produced result. An applied
effect may expose only argument-free `complete_goal`; rejection is read-only.
Same-goal identity is rechecked before evaluation and completion. Outgoing text
is not WorkingMemory, RuntimeState, persistent history, or reply-correlation
state. No autonomous cancellation, replacement, unbounded effect sequence, or wait for
a human is exposed. `previous_response_id` remains confined to a single tool
continuation and is never carried forward.

One independent request can be made without shell quoting:

```text
mira> ask Reply with exactly: cognition online
```

Immediately before each request, `RobotApplication` adds selected current
grounding to the provider instructions: profile identity, lifecycle, the latest
runtime-owned platform snapshot, hardware and body summaries plus authoritative
orientation, semantic presence, and camera resource metadata. An optional
startup prompt remains a separate operator-instruction section. The application,
not the OpenAI adapter, selects and renders this context. Camera metadata
indicates resource availability only: cognition cannot capture, access, or see
images unless image data is explicitly supplied. No frame is captured or sent.

The application also explicitly renders up to six prior completed cognition
interactions as a separate working-memory section. This session-local FIFO is
bounded and volatile; historical text is quoted as data, while the current
operator request, operator instructions, and fresh Runtime context have
precedence. Clearing memory or restarting the process removes this continuity.
The provider still has no cross-request conversation or retained session.
Each retained turn carries its application-captured completion time. A small
bounded observation projection separately retains acquisition/sample time for
time-sensitive evidence such as battery voltage; completion and observation
times are explicitly different facts, and no complete runtime snapshot is kept.
At most three observations with at most sixteen facts each are retained per turn;
observation metadata and values have fixed character limits and are deterministically
truncated. All observation strings are JSON-quoted when rendered as historical data.

Each ask also receives the current application-owned `ActiveGoal`, separately
rendered from Runtime context and Working memory. The provider has no goal,
task, or conversation store. Provider-neutral `set_goal` and `resolve_goal`
calls use the existing bounded runtime dispatcher; the OpenAI adapter remains
transport-only. A goal transition is re-grounded for the final response but
starts no autonomous cognition or action.

Responses function calling projects request-time semantic capabilities.
`orient_body` is offered only when the current body supports orientation and is
nonphysical; independently, `set_goal` is offered with no active goal and
`resolve_goal` with an active goal. The adapter offers strict numeric
`yaw_degrees` and `pitch_degrees` arguments, automatic tool choice, and disabled
parallel calls.
It transports at most one request to the runtime-owned dispatcher, which
validates untrusted arguments and invokes
`RobotApplication.set_body_orientation()`. The provider never receives a body
backend or mutable state. Physical body actuation is deliberately unavailable.

When Jobs persistence is configured, explicit operator dialogue also projects
`inspect_job_result(selector)`. This read-only acquisition retrieves one exact
terminal `RUN<n>`, the latest completed occurrence for `JOB<n>`, or a uniquely
resolved exact case-sensitive Job name. Its Job/JobRun identity, status, and
timestamps are runtime-owned durable metadata; its summary and report are
historical cognition-authored work product, not fresh current evidence or
persistent memory. The capability uses the ordinary two-acquisition operator
budget and is never projected into autonomous attention or bounded Job work.

After an applied or rejected request, the adapter sends the runtime-produced
function output and refreshed authoritative grounding for one final text-only
response. Its `previous_response_id` is local to that one ask; it is neither
retained nor reused and does not provide conversation memory.

Text cognition backends may perform backend-specific preparation before the
application announces readiness. The OpenAI adapter uses its single preparation
attempt to eagerly initialize and cache the client, then sends exactly one
minimal, tool-free Responses request with the fixed input `Reply ready.` using
the configured model. It supplies no instructions, runtime context, tools,
working memory, goal, prior response identifier, or operator data. The response
text and identifier are discarded, so preparation creates no attention episode,
conversation turn, memory, goal, persistence, or application effect.

`[APP] running` is logged only after preparation succeeds or reaches an
explicitly degraded result. A missing SDK, missing key, or provider failure is
an expected cognition-layer failure: it does not stop the rest of the runtime
and does not disable a later ordinary cognition attempt. Preparation has no
retry loop, periodic keepalive, special model, or prompt-cache optimization;
calling it again on the same OpenAI backend is a no-op, even if its one attempt
failed.

The adapter logs content-free `[COGNITION]` elapsed timings for that first local
client initialization and for each prewarm, initial, or tool-continuation
Responses API call. Provider-call ordinals are local to one backend instance,
and `cold=true` means only that the call is the instance's first outbound
provider request. A successful prewarm is therefore ordinal 1 and the first real
request is ordinal 2 with `cold=false`.
Request lines include character/tool counts and public numeric token usage when
the SDK supplies it; they never include prompt, result, identifier, or tool
content.
Independent requests gain continuity only because the runtime explicitly
supplies its working-memory snapshot. This phase adds no durable storage, task
manager, planning, retries, physical autonomous action, images, perception,
audio, streaming, or Realtime API integration.

For a physical cold-start check, restart the runtime and compare the startup
`component=client_init` and `provider_request=prewarm` lines before
`[APP] running` with the first `provider_request=initial` line after asking
`Say only: ready.`. This is a manual observation procedure, not a benchmark.

## Bounded continuation

Add `--initiative-continuation` only together with `--initiative`,
plus at least one of `--initiative-actions` or `--initiative-messages`; messages
still require today's `--console` transport. Scheduling supplies the base
temporal effect. Initiative Request A remains a one-tool request. After an
applied effect, the runtime may make exactly one new independent continuation
request with fresh Runtime context, the same captured ActiveGoal identity, the
same episode-start WorkingMemory snapshot, the original attention stimulus, and
the first runtime-produced effect result. Its tool projection is fresh and
excludes the first tool; its first call consumes its budget even when rejected.
There is no continuation after a rejected or absent first effect.

The optional outcome request follows the continuation and describes one or two
effect results. `complete_goal` is available only when every requested effect
applied and the same goal remains active. Ongoing maintenance goals are not
automatically completed. The OpenAI adapter is unchanged: A, B, and outcome C
are separate `respond()` calls. Any `previous_response_id` is confined to the
adapter's internal tool-result continuation for one call and is never carried
from A to B or B to C. This adds no planning, retries, pursuit loop, or provider
conversation state.

## Semantic attention grounding

Autonomous requests render a generic `Attention stimulus` containing one
provider-neutral `Semantic observation` with its kind, source, and ordered
facts. It is runtime-generated rather than operator input. Current Runtime
context and Active goal remain authoritative; WorkingMemory may be stale.
`--initiative-platform-attention` opts thermal-warning and memory-pressure
raised/cleared transitions into the same goal-gated, one-in-flight Phase 10
path. It adds no OpenAI tool, provider conversation, polling, or persistent
observation data.

## Self-inspection tool transport

The provider may receive the one exact `inspect_self(area)` tool. Tool-result
continuation remains scoped to that request; an autonomous post-inspection
decision is a fresh independent request without a previous response identifier.
See [Bounded semantic self-inspection](self-inspection.md).

## Temporal effect transport

The exact `schedule_followup(delay_seconds, purpose)` tool is an autonomous
semantic effect. Provider tool-result finalization is confined to the scheduling
request. No `previous_response_id` or request survives the wait; due-time
attention is independent and uses fresh runtime/capability/memory projections.
See [Bounded temporal follow-up](temporal-followup.md).

## Current request boundaries

See the [current bounded episode grammar](architecture.md#current-bounded-autonomy-episode-grammar).
Provider state and response IDs never cross a temporal wait. An acquisition-informed
request and the optional distinct effect continuation are independent calls with
different policy gates.

## Job Workspace operator tools

Explicit operator dialogue receives `workspace_list` and `workspace_read` when
both the durable Job catalog and Workspace store are available. They use the
ordinary two-acquisition episode budget, duplicate reuse, and existing
post-acquisition choreography. They are intentionally omitted from autonomous
initiative, automatic continuation, and bounded Job work because authored
Workspace prose is not authoritative outcome evidence. The tools expose bounded
logical paths only, never physical filesystem paths or writes.

Workspace metadata is a current runtime storage observation. Artifact content
is authored non-authoritative working material, distinct from BRD state,
durable JobRun results, and persistent memory. A Job Workspace can exist without
any active or completed JobRun.

Explicit operator dialogue also receives `workspace_write(job, path, mode,
content)` when both stores are available. All four non-null arguments are
required; mode is exactly `create`, `replace`, or `append`, and content has an
8,000-character cognition ceiling beneath the storage byte and quota limits.
It shares the exact Job resolver used by reads.

This effect is absent from acquisitions, diagnostics, initiative effects,
autonomous continuation, scheduled cognition, and bounded Job work. It may be
used only when the current operator request explicitly requests or clearly
authorizes the durable write, never for proactive note-taking. One write ends
the finite operator episode, even when it follows one or two acquisitions.

Only `status=applied`, `published=true`, and `durability_confirmed=true` justify
an unqualified success acknowledgement. Rejection or unavailability does not.
An indeterminate published result means publication may have happened but
durability was not confirmed; cognition must claim neither durable success nor
definite failure. Workspace prose remains non-authoritative, and writing it is
not persistent-memory admission or a JobRun-result mutation.
