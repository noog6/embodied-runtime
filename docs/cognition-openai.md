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

The adapter is initialized lazily on the first request. A missing SDK, missing
key, or provider failure affects that request only and does not stop the runtime.
This phase has no history, runtime-state projection, tools, images, audio,
streaming, or Realtime API integration.
