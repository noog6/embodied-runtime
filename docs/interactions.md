# Interaction identity

Phase 20.3 makes explicit operator dialogue interaction context authoritative,
request-scoped cognition grounding and uses its current channel to select a
bounded dialogue presentation policy without changing runtime authority.

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

Presentation policy grants no tools, removes no tools, and changes no attention
or acquisition budget. Legacy source-only cognition remains policy-neutral, and
notification presentation policy remains deferred.

The application continues to own one bounded volatile `WorkingMemory`. Historical
turns provide semantic continuity regardless of the channel on which they
occurred: switching between console and voice neither clears nor forks history.
Working-memory turns are not channel-tagged. A bounded voice-session boundary is
therefore not a general conversation boundary. Persistent conversation identity,
conversation IDs, and thread IDs remain deferred.
