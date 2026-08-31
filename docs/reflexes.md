# Local reflexes

A reflex is a small deterministic reaction from a semantic event to a semantic
application capability. Reflexes consume facts from the transient `EventBus`,
request capabilities through `RobotApplication`, and own no authoritative robot
state. They are local runtime behavior: no AI, cognition, planner, or direct
backend access participates.

`PresenceCenteringReflex` subscribes to `PresenceChanged`. When `present=True`
from any source, it requests body orientation at yaw `0.0` and pitch `0.0`.
`present=False` has no action. The source is intentionally irrelevant because
the event already represents semantic presence and contains no direction.

Repeated equal observations do not retrigger the reflex: presence ingestion
publishes `PresenceChanged` only for transitions. After an absent transition,
a later present transition centers again. Successful body requests update
authoritative `BodyState` through the normal application API; failed requests
leave it unchanged.

The application starts configured reflexes after the body is ready and before
it announces `ApplicationStarted`. It stops them before the body and hardware.
The current demo composition configures presence centering, while generic and
headless applications may configure no reflexes. A future physical
`BodyBackend` can therefore use the same reflex unchanged.
