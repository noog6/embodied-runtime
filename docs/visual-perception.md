# Active visual perception

Operator-supplied MMS images are direct current-interaction input, not visual scene
acquisitions. They are never synthetic `CameraFrame` values and do not acquire the
camera resource or increment camera-capture or vision-acquisition accounting. Only
the scene-inspection path represents a runtime-controlled observation through the
robot's physical camera.

Phase 14 adds the read-only cognition capability `observe_scene(focus)`. `focus`
is a trimmed, non-empty string of at most 300 characters without control
characters. It cannot select a camera, file, URL, crop, exposure, resolution,
provider, model, or frame count.

Each applied call captures exactly one current `CameraFrame` and passes it to
the configured `VisualPerceptionBackend`. The resulting
`VisualPerceptionResult` contains the normalized focus, a description bounded
to 2,000 characters, and an explicit truncation flag. Both visual adapters use
an in-memory base64 image data URL. Empty frames, unsupported media types, invalid
dimensions/timestamps, and frames larger than 4 MiB fail before any provider call.
JPEG, PNG, and WebP are accepted. Capture is never retried. A configured ordered
chain may try a later provider using that same frame; each entry is attempted at
most once.

Each remote attempt is non-streaming, has zero SDK retries, and requests at most
4,096 output tokens (`max_tokens` for compatible Chat Completions,
`max_output_tokens` for Responses). No tools, tool-choice settings, or parallel-tool
parameters are sent. A completed, nonempty prose result is required. A completed
description over 2,000 characters is locally shortened with `truncated=true`;
provider-truncated output is a failed attempt, never accepted as a complete
interpretation.

## Authority and lifetime

`CameraFrame` is a transient authoritative sensor payload.
`VisualPerceptionResult` is a model-generated interpretation of one frame and
may be incomplete, uncertain, or wrong. Its `observed_at` is sampled immediately
after capture, before interpretation, so model latency does not become scene time.
`RuntimeState` remains the authority for runtime facts. A bounded textual projection
may enter a completed operator turn's volatile working memory; it is never copied
into runtime state and is not an initiative effect. Image bytes are discarded after
the request; they are not written,
logged, published, cached, or retained as history.

The canonical exclusive resource for frame acquisition is `camera`, the first
physical capability integrated with `ResourceArbiter`. Every normal
application-owned capture goes through one application boundary: direct CLI and
console capture uses the stable `runtime:camera_capture` owner, while operator and
standalone autonomous visual perception use `runtime:visual_perception`.
Autonomous perception uses `task:<Task UUID>` only when its expected goal is the
exact `ActiveGoal` bound to the current running Task. A Task merely existing, or
a matching goal description, does not transfer that ownership.

The exclusive lease covers only synchronous physical capture and is released in
a `finally` block before frame-size validation or asynchronous interpretation.
Backend capture failure therefore cannot strand the one-shot lease. Contention is
a bounded, fail-fast rejected acquisition: there is no wait, retry, preemption,
or same-owner borrowing. It does not make the capability unavailable, because
availability describes configured runtime capability rather than instantaneous
authority. The camera adapter remains unaware of resource arbitration.

## Configuration and availability

Set `[runtime] vision = "openai-responses"` (or use the development override
`--vision openai-responses`) to enable remote interpretation. The historical
default is `none`. Effective non-`none` vision configuration requires a text
cognition backend.
A camera is required for `observe_scene`; [saved-image interpretation](workspace-images.md)
works without a camera. `OPENAI_VISION_MODEL` takes precedence over
`OPENAI_MODEL`; `OPENAI_API_KEY` remains environment-only.

Without a backend list, single-provider selection and model precedence remain
unchanged. Its requests now have a 30-second wall deadline, zero SDK retries, an
output-token limit, and strict completion/refusal checks. `[vision]
request_timeout_seconds` can set that deadline. Direct vision clients use the
OpenAI API endpoint explicitly, independently of `OPENAI_BASE_URL`.

For ordered fallback, copy your deployment configuration to a separate file,
replace its existing `[runtime] vision` value with `"configured"`, and append:

```toml
[vision]
request_timeout_seconds = 30

[[vision.backends]]
backend = "openai-compatible"
model = "google/gemini-3.8-flash"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
request_timeout_seconds = 15

[[vision.backends]]
backend = "openai-responses"
model = "gpt-6.1-sol"
api_key_env = "OPENAI_API_KEY"
request_timeout_seconds = 15
```

List order is priority. There are at most three entries; a supplied list must be
nonempty. Each entry requires an explicit backend, model, and credential-variable
**name**. Compatible entries also require an HTTP(S) endpoint without userinfo,
query, or fragment. Responses entries use the direct OpenAI endpoint and reject
`base_url`. Model names must be nonempty and contain no whitespace/control
characters. Both total and entry timeouts must be finite and positive; entry
timeouts default to 15 seconds, the total to 30. Unknown fields fail validation.
There is no discovery, model-name inference, or automatic model selection.

If `[runtime].vision` is absent, a backend list enables `configured` vision.
Explicit `[runtime] vision="none"` or CLI `--vision none` disables it. CLI
`--vision configured` enables an existing list. An `openai-responses` selection
combined with a list is a configuration error, including an explicit CLI override:
the runtime will not silently discard entries. Remove the list to return to the
legacy single backend. The cognition dependency check still applies; a camera is
optional for saved imagery.
Provider selection lives in deployment configuration, never `RobotProfile`.
The checked-in production deployment continues to use its existing Responses vision.

Compatible vision has its own explicitly keyed SDK client. Direct fallback has a
separate explicitly keyed client. Neither changes cognition, TTS, or other clients.
OpenRouter retains `require_parameters=True` and `allow_fallbacks=False`: runtime
fallback to the next configured entry is distinct from provider-side routing.
The selected compatible endpoint must support image input and `max_tokens`.

Source both protected credential files in the launching shell:

```sh
source ~/.config/embodied-runtime/openrouter.env
source ~/.config/embodied-runtime/openai.env
```

For systemd, the startup wizard selects all active vision entries' credential
variables, deduplicated with other deployment requirements. With those variables
and your existing TTS/SMS credentials loaded, use `check` and then explicitly
capture them with `install --capture-env`; see [startup deployment](startup.md)
and [secret delivery](secrets.md#ordered-visual-perception). Secure environment-file
handling is unchanged. A missing credential at runtime fails only that entry,
allowing a later configured entry to work. A first wizard capture still requires
all selected values; the wizard does not silently create an incomplete secret file.

A configured camera is not the same as remote vision being enabled. The tool is
offered only while the application is running, the configured camera is
currently running, and a visual backend is configured. Camera self-inspection
reports these resource and interpretation facts separately and never captures.

## Fallback policy and deadlines

The wrapper passes the exact same immutable frame or historical `ImageSnapshot`
(bytes, media type, dimensions, and original capture time when known) and focus to
each sequential attempt. The runtime holds the
original capture-time `observed_at` through the entire chain. No recapture, blending,
concurrent provider calls, or model-selected providers occur. Internal fallback
consumes no additional runtime acquisition slots and changes no Job/effect limits.

The overall interpretation deadline starts after capture. Each attempt receives
`min(entry timeout, remaining overall budget)`, including its SDK per-call timeout.
Elapsed time is not reset for a later entry. An exhausted total budget prevents
any later attempt from starting. Cancellation propagates immediately.

Eligible failures are timeout, connection failure, rate limit, provider outage,
missing credentials, unavailable SDK/backend, payment required/credit exhaustion
(HTTP 402), unavailable model/endpoint (HTTP 404/410), empty or malformed output,
and provider truncation. The next configured entry is tried once. Valid prose
expressing uncertainty is accepted immediately.

Structured safety refusals stop the chain, including content-filter finishes.
Invalid local input/configuration, authentication rejection (401/403), other invalid
requests, unclassified provider errors, cancellation, and unexpected implementation
errors also stop it. Programming exceptions propagate to the existing runtime
failure boundary; they are not silently converted into provider switching. If all
eligible entries fail, `observe_scene` returns a bounded rejected result categorized
`all_backends_failed`, with attempted failure categories and no raw provider body.

## Attempt accounting

`summary.json` schema version 5 adds a `vision_usage` array and separate
`vision_provider_requests`, `vision_provider_failures`, `vision_provider_duration_ms`,
`vision_fallbacks`, and `vision_*_tokens` counters. Existing cognition counters and
`provider_usage` retain their meaning. `provider_calls` contains both roles with
`role="vision"` or `role="cognition"`. Vision calls include backend, protocol, safe
endpoint origin, requested/returned model, attempt order, fallback occurrence,
status, duration, failure category, safe HTTP error status, normalized usage,
usage availability, and `accepted`. The accepted record identifies the provider
that supplied the result; `vision_attempt` events also cover setup failures.

Each outbound call counts exactly once, including failures and cancellations.
Usage from rejected output is retained. Setup failures before an outbound call do
not increment request counters. Missing required usage stays unavailable (`null`),
not zero; missing optional token counts remain `null`. Unknown/incomplete pricing
remains unavailable. Compatible calls never inherit direct OpenAI prices by model
name. Applicable complete direct usage has its own vision cost component. Images,
focus/instructions, provider bodies, reasoning, keys, and headers are not diagnostics.

See [operator validation procedures](portable-vision-validation.md) for fixed-image
primary/fallback checks and a normal camera-based request. Live Gemini/OpenRouter,
fallback, and Mira camera validation for this implementation are **unverified**;
automated validation uses fake clients and frames only.

## Autonomous bound

An autonomous attention episode can attempt at most two ordered read-only
acquisitions, using `inspect_self`, `observe_scene`, or the same capability twice
for materially different information. Rejected attempts consume their slot. After
the first, a fresh request receives one remaining acquisition opportunity plus
effects; after the second, a fresh request receives effects only. No acquisition is
possible after the first effect. The existing continuation may apply one distinct
second effect, so the semantic-effect ceiling remains two and outcome evaluation
remains one. All bounded visual grounding can inform continuation and outcome, but
remains model-generated, uncertain, request-local, and separate from authoritative
Runtime facts and effect outcomes.

Visual perception is on demand. There is no streaming, background or continuous
vision, polling, image history, visual memory, change detection, or new
attention source.

Operator and autonomous attention episodes may use one-frame visual perception as
a bounded acquisition. Its typed evidence remains model-generated, potentially
incomplete or uncertain, and not authoritative RuntimeState. It is not flattened
into runtime fact text when carried to a later acquisition stage.

## Retained saved images

`interpret_image(image_ref, focus)` uses the same configured backend and fallback
chain on immutable retained bytes. `ImageSnapshot` explicitly carries a nullable
original capture time, without synthesizing a new `CameraFrame` timestamp.
Remote instructions and results label it historical; interpretation time is
separate and the result is fallible. It consumes an ordinary acquisition.
`observe_scene` keeps its current-frame behavior. See [Workspace images](workspace-images.md)
for the full grammar, ownership, quotas and operator validation.
