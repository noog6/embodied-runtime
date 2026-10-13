# Compatible cognition and OpenRouter

The `openai-compatible` backend uses the existing optional OpenAI SDK and
non-streaming Chat Completions. Normal runtime startup and the benchmark CLI use
the same construction function. No orchestration library or provider session is
added. The checked-in `config/mira-agentic.toml` keeps OpenAI Responses.

## Keep OpenAI Responses

Install with Python 3.13 or later and source your protected OpenAI credential file:

```sh
python -m pip install -e '.[openai]'
source ~/.config/embodied-runtime/openai.env
python main.py --cognition openai-responses --cognition-model gpt-6.1-sol --console
```

`--cognition-model` overrides `cognition.model`, which overrides `OPENAI_MODEL`,
then the existing `gpt-5.6-luna` default. Existing vision and TTS clients retain
their own configuration and credentials.

## Configure OpenRouter

Keep `OPENROUTER_API_KEY` in a protected environment file outside the checkout,
using the [secret delivery procedure](secrets.md#openrouter-cognition). Never put
the credential value in TOML, command arguments, URLs, or logs. Create your own
configuration, for example `/tmp/mira-openrouter.toml`:

```toml
[runtime]
hardware = "virtual"
cognition = "openai-compatible"
mode = "console"

[cognition.openai_compatible]
model = "openai/gpt-6-luna"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
request_timeout_seconds = 30
tool_calling = true
image_input = false
parallel_tool_calls_mode = "disable"
```

This is an explicitly selected example model, not a runtime default or evidence
of a live benchmark pass. Before running, verify your selected model supports
client function tools using OpenRouter's [tool-capable model list](https://openrouter.ai/models?supported_parameters=tools)
and model documentation. `tool_calling = true` is an operator declaration;
validation makes no discovery or mandatory provider call. Missing model, URL,
credential variable name, or tool-capability declaration fails clearly. A missing
credential fails during preparation. Timeout must be finite and positive.

```sh
source ~/.config/embodied-runtime/openrouter.env
python main.py --config /tmp/mira-openrouter.toml
```

The compatible configuration never reads `OPENAI_MODEL` or `OPENAI_BASE_URL` and
passes its selected key explicitly to its own SDK client. Keep `OPENAI_API_KEY`
for any separately enabled existing OpenAI vision/TTS clients. Do not redirect
those clients to OpenRouter using global OpenAI environment settings.

Preparation makes one tool-free prewarm before runtime readiness. Each provider
call has a wall deadline as well as an SDK timeout; the SDK retry policy is
explicitly zero retries. An adapter call requests at most one tool, executes it
only through the existing runtime executor, then requests final prose with
`tool_choice="none"`. Final tool requests are rejected even if the endpoint
ignores this setting. A continuation failure propagates without restarting the
interaction or executing the tool again. Job acquisition, continuation, effect
budgets, tool argument validation, and fresh authority checks stay runtime-owned.

## Select the parallel-tool parameter mode

`parallel_tool_calls_mode` controls the outbound request shape:

- `"disable"` is the backward-compatible default. Initial and final requests
  carrying tool schemas send `parallel_tool_calls=False`.
- `"omit"` leaves that parameter out of both tool-bearing requests.

Tool-free prewarm never sends the parameter. Neither mode sends `True`.
Runtime and benchmark CLIs both accept
`--compatible-parallel-tool-calls-mode {disable,omit}`; an explicit CLI value
overrides TOML. Unsupported values fail local validation.

OpenRouter retains `require_parameters=True` and `allow_fallbacks=False`.
Strict routing checks whether an endpoint supports each supplied parameter;
`False` is still a supplied value. An endpoint advertising `tools` and
`tool_choice` without `parallel_tool_calls` can therefore be excluded even when
parallel calls are disabled, producing HTTP 404 with
"No endpoints found that can handle the requested parameters." Select `"omit"`
explicitly to leave this unsupported parameter out of routing. The adapter does
not infer the mode from model names, discover models, or retry with different
parameters after rejection.

Omitting the parameter permits routing without granting parallel execution.
In both modes the adapter rejects multiple returned tool calls before executing
any, executes one accepted request only through the runtime executor, and keeps
the final request at `tool_choice="none"`. Additional final-stage tool calls
remain rejected if the provider ignores that setting. Deadlines, cancellation,
zero SDK retries, and exactly-once execution after continuation failures remain
unchanged, as do runtime acquisition, continuation, and effect limits.

## Compare the same bounded Job

The following paid invocations exercise the same Contract Suite v1 scenario in
the production harness. Source each required credential file first:

```sh
python -m embodied_runtime.benchmarks \
  --backend openai-responses --model gpt-6-luna \
  --scenario confirmed_effect_requires_no_reverification --repeat 3 \
  --json-out /tmp/mira-responses.json

python -m embodied_runtime.benchmarks \
  --config /tmp/mira-openrouter.toml \
  --model openai/gpt-6-luna \
  --scenario confirmed_effect_requires_no_reverification --repeat 3 \
  --json-out /tmp/mira-openrouter.json
```

Equivalent explicit connection flags, useful without a TOML file:

```sh
python -m embodied_runtime.benchmarks \
  --backend openai-compatible --model openai/gpt-6-luna \
  --compatible-base-url https://openrouter.ai/api/v1 \
  --compatible-api-key-env OPENROUTER_API_KEY \
  --compatible-tool-calling --compatible-request-timeout-seconds 30 \
  --scenario confirmed_effect_requires_no_reverification --repeat 3 \
  --json-out /tmp/mira-openrouter.json
```

For Nick's post-merge check on Mira, run this normal production benchmark from
the checkout with its Python environment active. It needs no diagnostic wrapper:

```sh
source ~/.config/embodied-runtime/openrouter.env
python -m embodied_runtime.benchmarks \
  --backend openai-compatible --model anthropic/claude-opus-5.5 \
  --compatible-base-url https://openrouter.ai/api/v1 \
  --compatible-api-key-env OPENROUTER_API_KEY \
  --compatible-tool-calling --compatible-request-timeout-seconds 30 \
  --compatible-parallel-tool-calls-mode omit \
  --scenario confirmed_effect_requires_no_reverification --repeat 1 \
  --json-out /tmp/mira-openrouter-claude-opus-5.5.json
```

Runtime accepts those same connection/capability flags; select the model with
`--cognition-model` and backend with `--cognition`. Explicit CLI values override
the dedicated TOML table, then compatible defaults (30 seconds, images disabled,
tool capability undeclared, parallel-tool parameter mode `"disable"`).
`--no-compatible-image-input` and `--no-compatible-tool-calling` explicitly clear
capability declarations.
`[cognition].model` continues to belong to the Responses configuration. Other
runtime settings in a benchmark configuration are not applied to its isolated
virtual application. Existing `--model ... --repeat ...` benchmark invocations
continue to select Responses by default. All six scenario IDs and pass/fail
rules remain unchanged; see [benchmark scenarios](cognition-benchmarks.md).

## Read failures and usage

Benchmark format version 3 retains the existing trial, request, and tool-trace
fields and adds `protocol`, secret-free endpoint origin, `returned_models`,
`provider_calls`, `failure_category`, and `metrics.usage_available`. Each provider
call records requested/returned model, stage, status, duration, normalized raw
usage, usage availability, and failure category. Prewarm is excluded from trial
metrics and provider-call records. Protocol is `responses` or `chat-completions`.
Request and duration totals include every outbound attempt once, regardless of
response acceptance. Usage returned with refused, truncated, or malformed output
is retained in the aggregate totals. Timeouts and other attempts without reported
usage make token totals unavailable. Client setup failures before a call are not
counted as outbound attempts.
Endpoint identity includes scheme, hostname, and port; paths, credentials, queries,
and fragments are never diagnostic identity. URLs with userinfo, queries, or
fragments are rejected before client construction.

Missing required token usage is `null`, with `usage_available=false`; partial
per-call counts remain visible. A missing optional cached count is also `null`
in per-call usage and benchmark metrics. Estimated cost is separate and remains
`unavailable` unless the versioned catalog has applicable pricing and complete,
consistent usage. The built-in catalog has no compatible-endpoint prices: even
an OpenRouter model named like an OpenAI model receives no direct OpenAI rate.
No dynamic price lookup is performed. Provider response model identity is
diagnostic and does not trigger model routing or fallback.

Categories distinguish `authentication`, `payment_required`, `model_unavailable`,
`rate_limit`, `connection`, `timeout`,
`invalid_request`, `provider_unavailable`, `provider_error`, `unavailable`,
`empty_response`, `refused`, `truncated`, `malformed_response`,
`malformed_tool_call`, `multiple_tool_calls`, `unexpected_tool_call`, and
`cancelled`. Cancellation propagates as cancellation. Exceptions expose safe
categories, not provider error bodies or credential-bearing URLs. Inspect the
ordered runtime tool trace to see whether an effect succeeded before a final
provider response failed. A provider failure is not evidence that an already
confirmed runtime effect failed.

## Compatibility boundary

This milestone requires a model that accepts system/user messages, function tool
schemas and `tool_choice`, returns a single assistant choice and JSON-object
function arguments with stable tool-call IDs, and accepts matching tool-result
messages. Text output must be nonempty plain prose with a completed finish reason.
Refusal, truncation, malformed structures, multiple calls, legacy `function_call`,
and additional final-stage calls fail explicitly. No streaming, server tools,
parallel calls, fallback models, discovery service, or automatic capability inference
is implemented.

Optional images require `image_input = true` for a model documented to accept
image input alongside tools. The existing at-most-one-image attachment contract
uses an inline data URL and retains the image in request-local continuation.
Images are explicitly rejected when disabled and are never silently discarded.
No vision-provider or audio-provider refactor is included.

Responses continues using `previous_response_id` within one adapter call. Chat
Completions instead sends the original user request, assistant tool call,
matching tool-call ID, and runtime result. After execution, fresh authoritative
instructions replace the stale system message. OpenRouter's `reasoning_details`,
including opaque signatures, are echoed unchanged only in that local assistant
message; they never enter runtime memory, instructions, or diagnostic logs.
Tool schemas remain on the final request for router validation while tools are
disabled. OpenRouter requests require parameter support and disable provider
fallbacks; an unsupported parameter must fail instead of being silently ignored.

Official requirements checked against OpenRouter documentation on 2026-10-12:
[OpenAI SDK setup](https://openrouter.ai/docs/guides/community/openai-sdk),
[client tools and continuation schemas](https://openrouter.ai/docs/guides/features/tool-calling),
[reasoning details](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens),
and [required parameters and provider fallback](https://openrouter.ai/docs/guides/routing/provider-selection).
OpenRouter is the first supported integration; other compatible endpoints need
their own verification and may require unsupported metadata or parameters.
The adapter does not guarantee every model available through OpenRouter is suitable.

An operator reported one live diagnostic scenario pass for
`anthropic/claude-opus-5.5` using a temporary SDK wrapper that removed only
`parallel_tool_calls` from outbound requests. The same benchmark had failed with
the HTTP 404 above; the listed OpenRouter endpoints advertised `tools` and
`tool_choice` but not `parallel_tool_calls`. The wrapper run passed
`confirmed_effect_requires_no_reverification` **1/1**, with one work episode,
four provider requests, zero acquisitions, one effect, returned model
`anthropic/claude-opus-5.5`, and duration **23.9 seconds**.

That is one operator-observed scenario pass using the temporary wrapper.
The permanent configurable fix has been exercised with offline fake clients;
it has **not been live-validated**, nor does the supplied result validate other
models or the full suite. The normal command above is the post-merge validation
procedure.
