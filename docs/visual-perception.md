# Active visual perception

Phase 14 adds the read-only cognition capability `observe_scene(focus)`. `focus`
is a trimmed, non-empty string of at most 300 characters without control
characters. It cannot select a camera, file, URL, crop, exposure, resolution,
provider, model, or frame count.

Each applied call captures exactly one current `CameraFrame` and passes it to
the configured `VisualPerceptionBackend`. The resulting
`VisualPerceptionResult` contains the normalized focus, a description bounded
to 2,000 characters, and an explicit truncation flag. OpenAI Responses image
input uses an in-memory base64 data URL. A frame larger than 4 MiB is rejected
before any provider request, and neither capture nor interpretation is retried.

## Authority and lifetime

`CameraFrame` is a transient authoritative sensor payload.
`VisualPerceptionResult` is a transient, model-generated interpretation of one
frame and may be incomplete, uncertain, or wrong. `RuntimeState` remains the
authority for runtime facts. A visual result is request-local grounding only:
it is never copied into runtime state or working memory and is not an initiative
effect. Image bytes are discarded after the request; they are not written,
logged, published, cached, or retained as history.

## Configuration and availability

Set `[runtime] vision = "openai-responses"` (or use the development override
`--vision openai-responses`) to enable remote interpretation. The historical
default is `none`. Effective non-`none` vision configuration requires both a
camera and text cognition backend. `OPENAI_VISION_MODEL` takes precedence over
`OPENAI_MODEL`; `OPENAI_API_KEY` remains environment-only.

A configured camera is not the same as remote vision being enabled. The tool is
offered only while the application is running, the configured camera is
currently running, and a visual backend is configured. Camera self-inspection
reports these resource and interpretation facts separately and never captures.

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
