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

Add `--initiative` to opt into Phase 6 read-only attention. An autonomous pass
uses the same provider-neutral `TextCognitionBackend` boundary with a
runtime-generated request and attention section, but exactly `tools=()`, no
tool executor, and no refreshed-instructions callback. It creates no provider
conversation or background session and does not enter WorkingMemory. Requests
remain independent; `previous_response_id` stays confined to the continuation
inside one ordinary tool-enabled operator ask.

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

After an applied or rejected request, the adapter sends the runtime-produced
function output and refreshed authoritative grounding for one final text-only
response. Its `previous_response_id` is local to that one ask; it is neither
retained nor reused and does not provide conversation memory.

The adapter is initialized lazily on the first request. A missing SDK, missing
key, or provider failure affects that request only and does not stop the runtime.
Independent requests gain continuity only because the runtime explicitly
supplies its working-memory snapshot. This phase adds no durable storage, task
manager, planning, autonomous action, images, perception, audio, streaming, or
Realtime API integration.
