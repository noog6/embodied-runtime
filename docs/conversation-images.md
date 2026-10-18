# Joint image analysis and conversation recall

Operator image analysis accepts an ordered selection of up to **four images by
default**, in one provider request. JPEG, PNG and non-animated WebP are supported
for conversation inputs; Job workspace artifacts remain JPEG/PNG. Video, automatic
sampling, monitoring and observation between frames are outside this feature.

Examples:

- “Compare these two attached photos.”
- “Compare the last two photos I sent you this morning.”
- After restart: “Look again at yesterday’s plant photo and compare it with your camera now.”
- “Compare this workspace baseline with the current view.”
- “These four images are in chronological order. What visible action or change do they suggest?”

## Configuration and defaults

These optional tables can be added to an existing runtime TOML configuration:

```toml
[vision]
max_images = 4
max_total_bytes = 12582912       # 12 MiB encoded bytes per selected operation
max_total_pixels = 16777216     # 16 Mi decoded pixels across selected images
# Existing request_timeout_seconds and [[vision.backends]] remain supported.

[conversation_media]
enabled = true
# Explicit relative paths are relative to this configuration file.
database_path = "../data/conversation-media.sqlite3"
operator_scope = "primary_operator"
conversation_scope = "default"
max_age_days = 7
max_storage_bytes = 67108864    # 64 MiB live encoded media, across scopes in this DB
max_items = 256                 # Live images, across scopes in this DB
```

CLI composition enables retention by default at
`data/conversation-media.sqlite3` relative to the working directory unless an
explicit path is configured. Programmatic `RobotApplication` construction needs
an injected `ConversationMediaStore`; otherwise retention is unavailable. Retention
is independent of the dialogue-history enabled flag. Enable dialogue history too
for prior-turn text and image-ID linkage in historical context. Disabling retention
prevents new persistence and recall; it does not erase the existing database.
Re-enable the same scope to list and delete retained images.

Count must be an integer from 1 through 16; byte/pixel budgets must be positive
integers through 64 MiB/Mi pixels. These ceilings are configuration validation,
not a recommendation to raise the default on a Pi Zero. Each image remains limited
to 4 MiB, 4096 pixels per dimension, and 4 Mi decoded pixels. Decoding validates
actual raster bytes, MIME agreement and non-animation. The effective selection
limits apply to SMS ingress, operator inputs, tools, both cognition adapters,
remote vision, and the ordered fallback wrapper. Oversized selections fail;
no image is silently truncated or omitted. SDK-direct adapter calls use default
limits unless explicitly configured through `image_limits`.

## Storage, scope and lifetime

Accepted, validated inbound images are automatically retained before cognition.
SQLite stores bytes separately from its metadata table, atomically, with stable
`MEDIA…` IDs, operator and conversation scopes, channel, source message identity,
attachment index, receipt time, source, label, MIME type, dimensions, byte size and
SHA-256 integrity hash. Receipt time is never used as capture time. Inbound
capture time is unknown. All attachments in an SMS share the receipt timestamp
and keep their original order. No generated caption or visual embedding is stored.
Search uses the bounded accompanying operator text, not inferred visual content.

The database is separate from Job workspaces and uses mode 0600. Store it in a
trusted local directory. Configure the same scopes for the same authorized
operator across console, voice and authenticated SMS. A scope is deployment-owned;
models and inbound messages cannot choose it. This is a single-operator runtime:
local console/voice are trusted operator interfaces and Twilio's signature,
account, recipient and exact authorized-sender checks remain required. When
transferring a deployment to another person, change operator_scope and isolate
or erase the previous dialogue history too. The default principal name does not
automatically identify a human across unrelated installations.

Successful dialogue records include the retained IDs, with migration of older
history rows to an empty ID list. A separate media/request link is written before
analysis (at most eight recent request links per image). Image-only messages and
provider failures still leave media discoverable without a successful dialogue
turn. Metadata-only listing does not read image BLOBs. Only selected images are
loaded, integrity-checked and sent to a provider. Historical image bytes are never
automatically replayed in dialogue context.

Age is enforced at lookup even before physical cleanup. Cleanup runs at startup,
inbound retention and listing, deleting at most 32 expired payloads and up to 32
old tombstones per call. Idle processes do not run a cleanup timer. New images
fail persistence when the live item/byte quota is full; live media is not silently
evicted. Deleted/expired metadata keeps retry identity tombstones, bounded to four
times max_items per scope plus a bounded cleanup backlog while pruning catches up. SQLite
reuses freed pages, so the file size is a high-water mark including metadata and
journal overhead, not exactly the live-byte quota. Secure deletion overwrites
freed SQLite content; backups, storage-controller copies and dialogue text have
separate lifetimes.

The existing volatile Twilio dedupe cache still handles queued/current requests.
Retained source-message identities suppress image webhook retries after restart,
including after analysis failure, expiry or deletion while their tombstones remain.
A duplicate identity with different bytes is rejected. If persistence is disabled
or fails, restart dedupe cannot be guaranteed. The webhook queue is still volatile:
a crash after HTTP acknowledgement but before downloading/retaining a queued
image can lose it. Media retention does not claim to be a durable webhook inbox.

On disabled/unavailable persistence, disk failure, or quota exhaustion, current
images can still be analyzed. Runtime instructions identify retention status and
successful replies include a deterministic notice that the images were not saved
for later recall. No successful-save claim is made on failure. A later attachment
download failure attempts to preserve earlier valid attachments without analyzing
an incomplete comparison.

## Recall, analysis and deletion

`list_conversation_images(query?, after?, before?, limit?, offset?)` lists up to
16 actual records, newest receipt first, preserving attachment order within a
message. It supports bounded pagination and literal search over message text.
Time filters are offset-aware receipt times; natural-language dates are resolved
using the runtime operator timezone. Multiple matching plant photos are ambiguous
unless the operator supplied an ordering or selection rule. Cognition is instructed
to ask for clarification; matching metadata alone is not permission to guess the
visual identity of an image. Legacy text mentioning a photo supplies no pixels.

`interpret_image(image_refs, focus)` resolves an ordered list of exact retained
IDs, current `attachment:1` references, episode `img_…` references,
`workspace:JOB1:logical-path` selectors, and `camera`. The legacy
`interpret_image(image_ref, focus)` remains supported. Exactly one selector field
must be non-null. The camera is captured once per operation; repeated `camera`
selectors and all fallback attempts reuse those bytes. Its lease is released
before any provider request. Workspace resolution goes through the existing Job
selector and contained workspace loader. Autonomous Job work remains restricted
to its own acquired references and current Job camera authority; conversation
media is operator-only.

Listing then mixed resolution/analysis consumes the existing two acquisitions.
Bulk resolution of up to the configured image count is one bounded acquisition;
no general autonomy budget is increased. The result includes every selected
reference, label, source and known timestamp alongside the bounded interpretation.
Missing, expired, deleted, foreign-scope, corrupt or legacy-unretained selections
fail explicitly; the runtime never replaces them with a camera capture.

The exact operator command below deletes selected retained image bytes without
needing a model or authorizing any Job write:

```text
delete conversation images MEDIA0123456789abcdef0123456789abcdef
```

Use real IDs returned by listing. Up to 16 IDs can be separated by single spaces.
Deletion keeps bounded tombstones for idempotency. Retention never authorizes
`workspace_save_image`: its existing exact operator save grant still applies.

## Provider semantics and Pi resource trade-offs

Responses receives multiple `input_image` parts; compatible/OpenRouter Chat
Completions receives multiple `image_url` parts. Labels/metadata accompany each
ordered image and every fallback uses the identical selection and focus. Remote
attempts retain zero SDK retries and one total deadline. Local adapters that only
implement single-image perception keep working for one image and clearly reject
joint analysis. Configured vision models must actually support the selected
modalities/count; no model-name guessing or automatic discovery is added.

These payloads were verified against the official [OpenAI vision guide](https://developers.openai.com/api/docs/guides/images-vision)
and [OpenRouter image-input guide](https://openrouter.ai/blog/tutorials/send-image-to-llm/).
The supported openai-python 3.x Responses and Chat Completions types accept these
content arrays. Offline SDK serialization was exercised with openai 3.35.1;
validation used Pillow 12.3.0, Twilio 9.12.0 and aiohttp 3.14.5. Model/provider limits can be lower than runtime limits; operator
configuration must select a compatible vision model. Analysis prompts require
reference-grounded statements and distinguish visible differences from lighting,
viewpoint, occlusion and camera changes. Ordered images suggest changes, not
continuous observation or certainty about intervening events.

At the default aggregate byte limit, base64 adds roughly 4 MiB beyond 12 MiB of
encoded inputs, with further transient SDK/JSON copies. Raster validation decodes
one image at a time (up to 4 Mi pixels); immutable inbound attachments cache only
validated metadata to avoid repeated decoding during the turn. Validation still
adds CPU work on the Pi. Pixels
are not resized or re-encoded, preserving exact comparisons and hashes. More images
increase latency and provider tokens. Logs contain counts, references, duration,
fallback outcome and available usage; no pixel bytes, base64, provider bodies or
media URLs are placed in logs, ordinary history or EventBus payloads.

## Validation and remaining physical checks

Offline tests cover both payload formats and single-image compatibility, ordered
fallback, configured and aggregate limits, mixed sources, camera lease release,
SMS attachment ordering and retries, image-only/provider-failure retention,
restart recall, history/request links, scopes, ambiguity guidance, expiry,
deletion, missing/corrupt storage, retention failure and workspace authorization.
Run `python -m unittest discover -s tests`, `python -m compileall -q embodied_runtime tests`
and `git diff --check`. Tests use fake camera/provider boundaries; real raster
validation, SQLite, and local signed-webhook HTTP handling are exercised.

Physical/provider acceptance remains separate: on Pi Zero 2 W measure peak RSS,
validation/capture/provider latency and SD-card growth with four images near the
limits; verify camera lease release with the physical camera; send a real
four-attachment Twilio message and retry; restart and recall from another authorized
channel; compare a known baseline against the real camera through OpenRouter and
forced Responses fallback; check model count support and grounding under lighting,
viewpoint and occlusion changes. No physical actuation or live paid provider calls
were used for offline validation.
