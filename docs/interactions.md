# Interaction identity

Phase 20.2 makes explicit operator dialogue interaction context authoritative,
request-scoped cognition grounding without changing runtime authority or
presentation policy.

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
grounding. Channel-specific presentation policy is deferred to Phase 20.3.
