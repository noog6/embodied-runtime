# Interaction identity

Phase 20.6 adds operator-directed cross-channel text delivery while keeping the
current dialogue and autonomous notifications separate.

> **The interaction channel describes where the current conversation is happening.
> A delivery destination describes where the operator explicitly asked content
> to be sent.**

> **The channel carrying the conversation does not constrain where an explicitly
> requested authorized delivery must go.**

> **The operator may choose among runtime-authorized semantic destinations. The
> runtime owns the actual transport, recipient, account identifiers, credentials,
> and route.**

> **The model may decide whether to use an offered communication effect. The
> runtime decides where that autonomous notification is allowed to go.**

> **A channel being available for dialogue does not automatically make it
> available for autonomous notification.**

> **Dialogue expects an exchange. A notification delivers information.**

> **The channel belongs to the current turn. The conversation does not belong to
> the channel.**

Explicit operator dialogue interaction context is now authoritative
request-scoped cognition grounding.

- **Channel** says where communication occurs.
- **Interaction mode** says what kind of exchange it is.
- **Initiator** says who started the exchange.
- **`response_expected`** says whether the runtime owes a direct response to the
  initiating operator communication; it does not require a later human reply.
- **Source/provenance** says why a runtime-originated message exists. For example,
  `initiative` remains distinct from the console channel that delivers it.

| Channel | Mode | Initiator | Direct response expected |
| --- | --- | --- | --- |
| console | dialogue | operator | yes |
| voice | dialogue | operator | yes |
| console | notification | runtime | no |
| console | delivery | operator | no |
| console | administrative | operator | no cognition response |

Dialogue is the current conversational exchange. Notification is a
runtime-originated autonomous outbound communication. Delivery is
operator-authorized outbound content sent to an explicitly selected semantic
destination. One transport can carry multiple interaction modes.

The current eligibility matrix is deliberately small:

| Channel | Operator dialogue | Administrative | Autonomous notification | Operator-directed delivery |
| --- | --- | --- | --- | --- |
| console | yes | yes | yes | yes |
| voice | yes | — | no | no |

Console is the only current autonomous notification route. Voice remains a
fully supported operator-dialogue channel, but is not an autonomous notification
route or outbound delivery destination. This phase adds no unsolicited TTS.

The production delivery catalog contains only the semantic destination
`console`, described as the `local plain-text console`. When that route exists,
operator dialogue cognition receives `deliver_message(destination, message)`
with a runtime-generated enum containing only the catalog's authorized names.
With no route, the tool and its separate **Available operator delivery
destinations** grounding are absent. Availability is permission, not obligation;
cognition, rather than lexical intent parsing, interprets the request.

Each operator cognition stage uses the same captured semantic authority set for
its tool enum and grounding. Execution validates the captured name and then
re-resolves the current route. Removal or channel incompatibility rejects without
fallback; a compatible replacement under the same semantic name receives the
message. Sink objects and any future adapter's recipient, account, credentials,
and transport configuration remain runtime-owned and invisible to cognition.

A voice request may deliver standalone plain text to console while the current
interaction remains voice dialogue and its final response remains spoken. The
outbound identity is console/operator/delivery/no-response and preserves
`source="voice"`; console requests preserve `source="console"`. Delivery consumes
the existing single non-acquisition operator effect opportunity and creates no
second attention episode or WorkingMemory turn.

An applied result means only that the configured route accepted the message, not
that a person read, saw, acknowledged, or ultimately received it.
`address_operator` remains the autonomous message-only notification effect with
runtime route selection and `source="initiative"`; `deliver_message` is not
projected into autonomous, continuation, or outcome cognition.

Cognition authority and delivery semantics are separate concerns.

Interaction identity is request-scoped grounding, not robot state.

Operator dialogue describes the current interaction itself. Autonomous cognition
is not a notification interaction: it retains its attention identity, stimulus,
goal binding, budgets, and lifecycle, but may have an outbound notification
opportunity. When and only when `address_operator` is actually in a cognition
stage's projected tools, its instructions identify an **Available operator
notification** and the matching notification policy. The grounding describes the
effect's destination; it does not describe the autonomous episode.

The authoritative notification-route resolver evaluates the configured
`OperatorMessageSink` channel. Initiative messaging must be enabled, a sink must
exist, and its channel must resolve to an eligible route before `address_operator`
is projected. Cognition sees only that already-selected real destination, not a
menu of channels, and the tool schema contains only `message`; the model cannot
select a channel.

The resolved semantic route is captured once for the episode, using the same
runtime-notification identity later attached to `OperatorMessage`. Thus initial,
refreshed, post-acquisition, final-effect, and continuation instructions use a
stable destination wherever `address_operator` remains available. A sink alone
does not expose grounding when policy has withheld the tool. Tool authority and
delivery semantics remain separate.

At execution, the application obtains the currently configured sink, resolves
its eligibility again, and requires its semantic route to match the captured
route. A replacement sink on the same eligible channel may deliver the message;
the stale sink object is not retained. If the sink disappears, changes channel,
or becomes unsupported, delivery is rejected. There is no silent rerouting,
fallback, retry, queue, or persistence policy. Multi-transport routing remains
deferred until another real transport exists.

The supported console notification is asynchronous, self-contained plain text in
the local terminal. It is not an operator dialogue response, does not create or
extend a conversation, and does not wait for a reply. Its
`response_expected=false` means the delivery opens no dialogue turn and reserves
no cognition slot; it neither forbids a later operator utterance (which is a new
operator episode) nor prevents a genuine request for human action such as
connecting power.

Notification delivery does not request cognition, create operator attention,
start voice interaction, or append a turn to `WorkingMemory`. It remains the
output of the already-authorized autonomous episode. The existing console queue
and notification presentation remain responsible for asynchronous display.
Reply binding, notification persistence and history, acknowledgement tracking,
voice notifications, model channel selection, fallback, and routing among
multiple production channels remain deferred.

Console dialogue exposes `channel=console`, while voice dialogue exposes
`channel=voice`; both expose the same dialogue mode, operator initiator, and
expected-response semantics. The context neither grants nor removes tools and is
not persisted. Legacy source-only calls do not invent richer interaction
semantics. Administrative interactions remain local, and notification interaction
identity remains outbound delivery semantics rather than operator cognition
grounding.

For explicit operator dialogue, the request-scoped channel selects policy for
the response being formulated. Voice policy optimizes for spoken comprehension:
natural, conversational language is preferred over screen-dependent formatting,
while exact textual values remain available when explicitly requested. Console
policy optimizes for textual consumption in a plain terminal and permits useful
structure and exact technical strings without requiring either Markdown or
verbosity. The interaction identity and its policy remain stable throughout all
cognition and acquisition stages of that operator episode.

Presentation policy grants no tools, removes no tools, and changes no attention,
acquisition, or effect budget. Legacy source-only cognition remains policy-neutral.

The application continues to own one bounded volatile `WorkingMemory`. Historical
turns provide semantic continuity regardless of the channel on which they
occurred: switching between console and voice neither clears nor forks history.
Working-memory turns are not channel-tagged. A bounded voice-session boundary is
therefore not a general conversation boundary. Persistent conversation identity,
conversation IDs, and thread IDs remain deferred.
