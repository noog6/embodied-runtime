# Bounded temporal follow-up

> **Future intent is not a temporal commitment. A temporal commitment exists
> only when authoritative runtime temporal state says it exists.**

> **Natural-language timing inside an active goal does not schedule anything.**

Goal wording, WorkingMemory, and prior assistant text can describe future intent,
but none proves a timer exists. Only authoritative `pending` or `due_pending`
state, or a successful runtime-produced `schedule_followup` result, confirms a
commitment. A rejected request must not be described as scheduled.

The runtime provides one provider-neutral semantic effect:

```text
schedule_followup(delay_seconds, purpose)
```

`delay_seconds` must be an actual JSON integer from 10 through 86400 (a boolean
is not an integer here). `purpose` is trimmed, must contain 1–300 characters,
and may not contain Unicode control characters. Extra arguments are rejected;
values are never clamped or truncated.

## Ownership and timing

`TemporalFollowupController` owns zero or one immutable `PendingFollowup`. It
contains the relative delay, bounded purpose, exact `ActiveGoal` object, and an
internal monotonic due point. The pending commitment is separate from
`RuntimeState`, `ActiveGoal`, `WorkingMemory`, and sensor/perception payloads.
It is volatile process state: stop, restart, or power loss discards it. It is
not written to disk, configuration, environment, memory, or a database.

The controller creates one asyncio task using a monotonic relative sleeper. No
cognition call, provider response, LLM connection, or `previous_response_id`
remains alive during the delay. The sleeper and monotonic clock are injectable
for deterministic tests. Phase 15 has no absolute timestamps, dates, time
zones, calendar, cron, recurrence, queue, identifiers, or timer collection.

Scheduling requires a RUNNING application, enabled initiative, a current goal,
and no pending follow-up. It captures the exact current `ActiveGoal` object.
Clearing, resolving, or completing that goal cancels the task. Shutdown also
cancels and awaits it. At due time, RUNNING state and exact object identity are
checked again; stale work is discarded without an event.

## Explicit operator scheduling

Operator cognition is offered `schedule_followup` only while the application is
RUNNING, initiative is enabled, an exact active goal exists, and no follow-up is
already pending. The effect uses the same executor and
`TemporalFollowupController` as autonomous cognition. Execution rejects the
request if the goal object grounded for that operator cognition stage is no
longer the exact current object; a replacement goal is never silently bound.

The bounded workflow may intentionally require two turns:

```text
operator turn 1:
  establish active goal

operator turn 2:
  explicitly request one schedule_followup effect
```

Operator cognition still permits at most two read-only acquisitions followed by
at most one non-acquisition semantic effect. Setting a goal and scheduling a
follow-up therefore cannot occur as two effects in one episode. The applied tool
result confirms scheduling, and freshly reconstructed `TemporalSituation` is
authoritative afterward.

The commitment remains single, one-shot, and session-local. This workflow adds
no replacement, rescheduling, or recurrence.

The due event carries that same goal reference only as an internal identity
fence. Attention checks it again when consuming the event, closing the race in
which a replacement goal could appear after publication. The reference is not
projected, serialized, persisted, or exposed to cognition, and creates no ID.

## Due-time attention

A valid due timer changes the commitment to `due_pending` and publishes exactly one
`TemporalFollowupDue(source="temporal_followup", purpose, delay_seconds)`.
Its semantic projection has kind `temporal_followup_due`, the same source, and
only purpose/delay facts. No task data or internal monotonic timestamp is
projected.

Attention subscribes whenever initiative is enabled, independently of platform
attention. Due starts a completely new episode with fresh `RuntimeState`, the
same current goal object, current capabilities, and a current WorkingMemory
snapshot. The prior request reserved no authority. Autonomous episodes still do
not append to WorkingMemory. If another episode is in flight, the temporal controller retains its single
`due_pending` handoff. When that episode ends (including provider failure),
attention rechecks RUNNING state and exact goal identity, atomically releases the
slot, and accepts one fresh episode without an intervening event-loop suspension.
If another observation has already started an episode, the slot remains
`due_pending` until that episode ends. A queued event whose commitment was
cancelled cannot be claimed and starts no cognition. Other observation types remain lossy; this is
not a general queue, replay, retry, priority, or recurrence mechanism.

`schedule_followup` is a semantic effect, not read-only acquisition. It consumes
one of the existing maximum two effect positions. An episode may make at most two
ordered read-only acquisition attempts, using `inspect_self` or `observe_scene`
according to the bounded acquisition grammar; acquisitions do not consume the
semantic-effect budget. Successfully applied scheduling remains authoritative if
provider finalization fails, and no continuation, outcome evaluation, or retry follows.

There is no automatic recurrence. Once the due episode is accepted and clears the slot, fresh cognition may
explicitly request one new follow-up if the same current goal still warrants it.

## Operator diagnostics

`followup` reports `none`, `pending`, or `due_pending`, plus bounded delay, remaining seconds, and purpose.
`followup clear` cancels the pending commitment. Runtime self-inspection exposes
only `temporal_followup_pending=true|false`; it does not expose task internals.
Structured `[TEMPORAL]` logs report schedule, due, and cancellation reason but
never purpose text.

A due follow-up cannot overlap an operator episode. The temporal controller keeps
the due commitment pending; when shared deliberative attention becomes idle it
may create a new, later-ID autonomous episode if the exact bound goal is still
current. It never reopens the scheduling episode, and waiting operators take
precedence over new autonomous work.
