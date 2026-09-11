# Interaction identity

Phase 20.1 explicitly represents communication identity without changing runtime
behavior or policy.

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

Phase 20.1 does not yet expose interaction context to cognition or change policy.
It does not change attention identity, memory admission, tool projection, budgets,
conversation behavior, or presentation.
