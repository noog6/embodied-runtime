# Interaction identity

Phase 20.4 keeps explicit operator dialogue interaction context authoritative and
adds authoritative, bounded grounding for an available autonomous notification
effect without changing runtime authority.

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
| console | administrative | operator | no cognition response |

One transport can carry multiple interaction modes.

Cognition authority and delivery semantics are separate concerns.

Interaction identity is request-scoped grounding, not robot state.

Operator dialogue describes the current interaction itself. Autonomous cognition
is not a notification interaction: it retains its attention identity, stimulus,
goal binding, budgets, and lifecycle, but may have an outbound notification
opportunity. When and only when `address_operator` is actually in a cognition
stage's projected tools, its instructions identify an **Available operator
notification** and the matching notification policy. The grounding describes the
effect's destination; it does not describe the autonomous episode.

The notification channel is captured once for the episode from the configured
`OperatorMessageSink`, using the same runtime-notification identity later attached
to `OperatorMessage`. Thus initial, refreshed, post-acquisition, and continuation
instructions use a stable destination wherever `address_operator` remains
available. A sink alone does not expose grounding when policy has withheld the
tool. Tool authority and delivery semantics remain separate.

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
voice notifications, channel selection, fallback, and routing among multiple
channels are deferred; outbound communication/routing policy belongs to Phase
20.5.

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
