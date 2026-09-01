# OpenAI text cognition setup

OpenAI Responses support is optional; the ordinary runtime needs neither the
SDK nor an API key. Install the project with only this optional integration:

```sh
python -m pip install -e '.[openai]'
```

Set `OPENAI_API_KEY` in the environment and do not commit secret values. The
default experimental model is `gpt-5.6-luna`; `OPENAI_MODEL` may override it.
Then start the local console:

```sh
python main.py --cognition openai-responses --console
```

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

Responses function calling exposes exactly one semantic capability,
`orient_body`, only when the current body supports orientation and is
nonphysical. The adapter offers strict numeric `yaw_degrees` and
`pitch_degrees` arguments, automatic tool choice, and disabled parallel calls.
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
Requests have no cross-request history or continuity. This phase adds no other
tools, images, perception, audio, streaming, or Realtime API integration.
