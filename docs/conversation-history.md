# Conversation history and request continuity

Mira keeps three separate forms of conversational memory:

- **WorkingMemory** is volatile, bounded recent dialogue, tool outcomes, and observations.
- **Conversation history** is durable completed operator dialogue, with bounded automatic
  projection and deliberate operator-only retrieval.
- **Persistent memory** contains semantic knowledge admitted through its existing
  authority boundary. Dialogue and retrieved history are never automatically admitted.

Historical dialogue is authored material. It can resolve “what was the second option?”
without becoming a current instruction, semantic truth, observation, proof of delivery,
permission to send, or authority to resume work. Tool results encode dialogue as JSON
strings beneath an explicit warning. Current Runtime and InteractionContext remain
controlling: recalling voice dialogue during SMS does not change the reply destination.

## Automatic context and deliberate recall

Automatic selection remains lightweight: three most recent prior-session turns from
this channel and two from other channels, merged chronologically with record ID as the
tie breaker. Current-session turns are normally represented by WorkingMemory alone.

When conversation history is enabled, explicit bounded operator dialogue additionally
receives these read-only acquisitions:

| Tool | Arguments | Result |
| --- | --- | --- |
| `search_conversation_history` | `query`, `limit` | Older completed dialogue across channels and sessions, including same-process working-memory evictions |
| `discover_requests` | `work_selector: "JOB1"`, `"RUN2"`, or `null`; `before: REQ…` or `null`; `limit: 1..5` | Page retained requests and their explicit work links, including older administrative requests |
| `inspect_request` | `selector: "recent"` | Five most recently accepted request references, including failed and administrative interactions |
| `inspect_request` | `selector: "CONV41"` or exact `REQ…` | Completed dialogue, response preparation, exact causal work links and retained delivery evidence |

Search uses SQLite FTS5 over existing stored dialogue, without a provider call,
embeddings, external service, or model-generated index. Queries contain 1–200
characters and 1–12 Unicode words; punctuation is ignored, words are literal and
joined by AND. FTS operators are not accepted as executable query syntax. For example,
`enclosure aluminium` matches both words; `OR` is an ordinary word. Control characters,
empty/no-word queries, oversized queries, and invalid limits are rejected.

Results are newest first by completion timestamp, then descending immutable record ID.
Each carries `CONV<n>`, request reference when available, runtime session, channel,
completion timestamp in UTC, and operator/assistant excerpts. Each excerpt is at most
600 characters, each call returns at most five matches, and each new acquisition
projects at most 12,000 characters. Exact inspection can return the retained 2,000
characters per side of one completed dialogue; pathological escaping that exceeds the
projection bound omits that dialogue's text and marks `projection_truncated`.
Search omits records already automatically projected or represented by request IDs in
WorkingMemory, and avoids repeating matches within the episode. Legacy volatile turns
without request references cannot be deduplicated by identity.

These tools use the existing two-acquisition operator episode budget, stage capability
validation, tool-result convention, and duplicate-call cache. Repeated identical
arguments (including JSON key reordering) reuse evidence without another database read.
No full archive is injected into every prompt. Job work, autonomous initiative,
startup/prewarm, reflexes, and vision-only requests receive no archive search or request
inspection capability. Existing Job/Workspace and semantic-memory boundaries remain.

Example: Nick discusses wood and ventilated aluminium enclosures over voice. After
several unrelated turns evict that exchange, he asks by SMS what the second option was.
Mira can search `enclosure`, identify the earlier voice exchange and its timestamp, and
attribute “ventilated aluminium” to that discussion. The same operation works after a
restart, while the record remains retained. A quoted historical “send me a message”
never authorizes a send in this new episode.

## Durable identities and work links

One admitted operator cognition request gets a runtime-generated `REQ` plus UUID
before cognition can invoke effects. A failed or cancelled request can have an identity
without a completed dialogue record. Successful dialogue gets a separate monotonic
`CONV<n>` and references its request. Runtime session identity is independent.
Console `job …` administrative interactions also get request references; their command
reports do not masquerade as completed dialogue. When history is enabled, their
printed output includes `Request: REQ…`, so Nick can cite the interaction directly.

To answer “what happened to the Job I started earlier?”, `discover_requests` can
filter explicit definition/start links with `work_selector: "JOB1"` or an exact
`"RUN2"`. Use `null` to page all retained requests when the Job is unknown. Each
page contains at most five requests and eight work links per request, newest
insertion first, independently of clock ties or rollback. Pass `next_before` as
`before` for the next page; no offset or whole-history cache accumulates. Inserts
after a page do not shift its continuation. A pruned cursor returns `cursor_expired`
without falling back to recent requests. Each page is one ordinary read-only
acquisition, available only in explicit operator dialogue. Filtering work links
does not infer that a report was sent, and report deliveries are not work-start links.

For example, after six unrelated dialogue exchanges, use
`discover_requests({"work_selector":"JOB1","before":null,"limit":5})`, then inspect
the returned exact `REQ…`. The earlier console start remains discoverable after
restart while retained, even though it has no searchable dialogue.

Only explicit runtime calls create work links:

- `job add` links `definition_created` to the new Job, with no Run ID.
- `job start` links `run_started` to the exact Job and newly created Run before Task
  activation. If activation fails, inspection follows that occurrence's actual status.

Scheduled/event occurrences are not retroactively attributed to the request that
created their Job. Child asynchronous work does not inherit an operator's causal
recording scope. There is no prose matching, nearest-time inference, latest-run
fallback, model-supplied link creation, or separate Job lifecycle authority.

`inspect_request` resolves each linked Run by exact ID and validates its Job ID. It
returns current durable status, start/finish timestamps, and report availability;
reports stay in the Job store. Follow its `RUN<n>` through existing `inspect_job_result`
or `retrieve_report` to read retained work. Missing stores/runs are unavailable and
never replaced with an unrelated result. Interrupted Runs remain interrupted.
Finding a discussion, inspecting its request, and retrieving a report can require more
than one operator episode under the unchanged two-acquisition budget.

These references assume the same paired conversation/Jobs databases. They are not
portable identities across independently replaced Job catalogs. Keep their backups
paired; replacing a catalog with reused numeric IDs cannot be detected by these links.

## Response preparation and delivery evidence

`prepared_at` records authored response/output preparation, independently of dialogue
persistence and transport outcome. The returned text carries its exact request
correlation in-process; transports do not consult a global “latest reply”. Delivery
metadata contains no duplicate message body. Each attempt has a separate `DEL` UUID,
request reference, channel, kind, attempted timestamp, outcome-recorded timestamp,
status, optional validated Twilio Message SID, and a fixed sanitized failure category.
Raw exceptions, arbitrary provider values, phone numbers, credentials, bearer URLs,
provider payloads, images and audio are excluded.

Report submissions have `kind: report` and a separate bounded `report_source`.
It is copied from the runtime-retained snapshot, never parsed from message text:

- Job result: `source_kind: job_run_result`, exact `job_id` and `run_id`.
- Workspace artifact: `source_kind: workspace_artifact`, `job_id`, logical `path`
  (at most 240 UTF-8 bytes), and the exact 64-character SHA-256 `content_version`.

The source is written with attempt intent before enqueueing/submission and stays on
that same attempt as console evidence advances from `queued` to `terminal_written`,
or SMS becomes accepted, rejected, or uncertain. Delivering the result from RUN2
therefore remains attributable to RUN2 after restart, even if RUN3 later completes;
`work` can correctly remain empty. Changing a Workspace file after retrieval does
not change the source version recorded for the retained bytes actually submitted.
This adds no report body or second work relationship. A source reference identifies
content; it does not prove receipt, guarantee the source is still retained, or grant
access to otherwise unavailable material. Legacy `report_source: null` means source
evidence unavailable; prose is never used to fill it in.

| Status | What the retained evidence establishes |
| --- | --- |
| `provider_accepted` | The Twilio API call returned successfully; no handset receipt or human reading is established |
| `provider_rejected` | The existing adapter's definite rejection classification applied |
| `submission_unconfirmed` | An attempt was prepared/entered, but external outcome is unknown (including timeout, cancellation, crash, or failed outcome persistence) |
| `queued` | The local console accepted a message into its volatile queue; terminal output is pending |
| `terminal_written` | The terminal write returned; no human reading is established |
| `playback_completed` | The configured TTS/playback adapter returned; nobody is proven to have heard it |
| `not_submitted` | The runtime withheld an empty SMS response |

Console queue evidence advances to terminal-write evidence only when the console
actually writes. Direct console `execute`/`execute_async` calls that return a report
without running the terminal loop do not invent a terminal receipt. SMS/MMS effects
inside an operator episode and direct SMS replies use the actual gateway boundary;
voice uses the bounded interaction's playback boundary. An oversized SMS reply's
existing deterministic replacement is marked `output_variant: sms_length_explanation`
before submission, including uncertain attempts (and `failure: response_replaced` on
acceptance); the ledger
does not claim the authored full reply was submitted. No replacement body is duplicated.
Existing synthesis-only fallback policy is unchanged.

Autonomous notifications have no originating operator request and receive no invented
request link. The ledger is request continuity, not a general transport audit service.
Legacy dialogue, pruned request metadata, and missing attempts explicitly mean delivery
evidence is unavailable/unknown. Missing receipt never establishes failure.

Intent is persisted **before** external submission. A crash between that write and
submission therefore conservatively leaves `submission_unconfirmed`, even if nothing
left the runtime. A crash after external submission but before outcome persistence is
also unknown. Opening the database does not manufacture a confirmation, replay an
attempt, poll receipts, resume a Job, trigger cognition, or send anything.

Recording is best effort under the existing dialogue error policy: failures log only
operation and exception type. Failed intent persistence does not prevent an authorized
send; failed outcome persistence cannot turn a successful send into failure or trigger
another send. Consequently there can be missing evidence, and there is no atomic
transaction spanning SQLite, the independent Job database, and external transport.
The application owns and closes this store on shutdown. No Twilio callbacks, retries,
receipt polling, unsolicited SMS, or automatic Job-result delivery were added.

## Configuration, migration, retention and cleanup

Existing configuration is unchanged. Absence of this section, or `enabled = false`,
disables durable dialogue and continuity storage and the new retrieval tools:

```toml
[conversation_history]
enabled = true
database_path = "../data/conversations.sqlite3"
```

Relative paths resolve from the configuration file. SQLite must support FTS5. Schema
v3 transactionally migrates v1, preserving dialogue IDs, timestamps, channels, and text,
adding nullable request references and separate request/work/delivery tables, and
building an external-content FTS index. Legacy request/delivery evidence remains null;
no receipts or causal links are backfilled. Opening v2 adds a nullable report-source
column and a bounded work-lookup index without rewriting request references,
work links, outcomes or the existing FTS index. Existing delivery sources stay
unavailable; no JobRun is inferred or retroactively linked. Unknown versions and
unversioned nonempty databases are rejected. Opening/migration failures retain the existing composition
failure policy; runtime read/write failures are best effort.

Retention is a fixed small-device bound: at most 2,000 dialogue rows (2,000 characters
per side), 2,000 requests, eight work links and eight delivery attempts per request.
Report-source metadata lives on its delivery attempt and is deleted with it;
there is no separate orphanable source ledger. Oldest inserted rows are pruned on
opening and on request/dialogue insertion. IDs are never reused after pruning. Request pruning cascades work/delivery deletion and nulls
any surviving dialogue reference, explicitly leaving evidence unavailable. Dialogue
pruning deletes its FTS entries; failed requests without dialogue age out too. Metadata
has no growing in-memory registry: caches and exclusions are episode-local and bounded;
WorkingMemory remains its existing bounded FIFO.

SQLite reuses freed pages; pruning is not secure erasure and does not automatically
shrink a database's filesystem high-water size. No vacuum runs on the response path.
Deleting the closed conversation database removes its dialogue, search index and
continuity metadata together; it does not delete Jobs or semantic memory. Keep a backup
before migration or manual cleanup. Data outside the retained/truncated window cannot
be reconstructed, and lexical search is not semantic understanding.

## Nick's physical acceptance checklist

1. With history enabled, discuss two enclosure options over voice, exchange at least
   seven unrelated turns, then ask by SMS for the second option. Confirm attribution
   to the earlier voice exchange and its timestamp. Restart and repeat the question.
2. Use console `job add` and `job start`, finish that occurrence, then start/finish a
   later occurrence. Ask Mira to inspect the earlier start request and retrieve its
   linked exact Run. After six unrelated exchanges, discover the old start by Job
   filter or pagination and confirm the printed request reference agrees. Deliver an
   exact retained report and inspect its persisted source after restart; confirm
   the later report is not substituted.
3. **Live Twilio verification:** request an ordinary SMS reply and an explicit MMS.
   Inspect each originating request. Confirm provider acceptance is described without
   claiming handset receipt, and the normal reply stays on its originating channel.
4. **Live audio verification:** complete a voice turn and inspect its request. Confirm
   the record says playback completed without claiming anyone heard it. Provider
   timeouts/cancellation and storage failures are covered by fake-provider tests;
   safely controlled live fault testing must still never cause an automatic resend.
