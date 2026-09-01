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
not the OpenAI adapter, selects and renders this context. Camera metadata does
not capture or send a frame.

The adapter is initialized lazily on the first request. A missing SDK, missing
key, or provider failure affects that request only and does not stop the runtime.
Requests have no conversation history or continuity. This phase adds no tools,
actions, images, perception, audio, streaming, or Realtime API integration.
