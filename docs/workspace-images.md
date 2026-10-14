# Bounded Job Workspace images

A Job Workspace can hold text and single-frame JPEG/PNG artifacts. Capturing,
saving, interpreting, and sending remain distinct operations. Saving and sending
make no vision request. There is no automatic retention, generated caption,
image editing, comparison, semantic search, inbound-MMS persistence, memory
admission, or change to nightly Jobs.

## Tool grammar and budgets

All arguments shown are required. Operator Job selectors retain the existing exact
`JOB<n>` or exact, case-sensitive Job-name resolution. No tool creates a Job or
chooses a save destination. Paths are Job-relative logical paths; extensions are
labels, not format evidence. No tool returns encoded bytes, base64, physical
paths, or arbitrary file/URL access.

| Tool | Operator arguments | Current Job work arguments | Class |
| --- | --- | --- | --- |
| `capture_camera_image` | `{}` | `{}` | acquisition |
| `workspace_load_image` | `job, path` | `path` | acquisition |
| `interpret_image` | `image_ref, focus` | `image_ref, focus` | acquisition |
| `workspace_save_image` | `job, path, mode, image_ref` | `path, mode, image_ref` | effect |
| `deliver_image` | `image_ref, destination, caption` | unavailable | effect |

`mode` is `create`, `replace`, or `upsert`, with the same absence/presence rules
as text operations. Images cannot be appended. `focus` is nonempty and at most
300 characters, without controls; interpretations are bounded to 2,000 characters.
`caption` is a separate explicit delivery argument, at most 320 characters;
use `""` when no caption is requested. Captions/descriptions can be separate
ordinary text artifacts when explicitly requested; nothing generates or saves them
automatically.

The existing two-acquisition ceiling and effect grammar are unchanged. Operator
work permits one effect; Job work uses its existing effect and continuation
opportunities. There is no image-specific loop. Capture → save needs one
acquisition and one effect. Load → interpret needs two acquisitions. Load → send
needs one acquisition and one effect. Capture → save → interpret → send exceeds
one episode's grammar and must be split across explicit episodes.

Capture requires a running camera and uses its existing fail-fast resource lease.
Load requires Jobs and Workspace persistence. Interpret is offered after acquiring
an image reference and requires configured vision, independently of camera
availability. Operator save is offered only when a retained image can be used and the current
operator request grants an exact durable-write authorization. Delivery requires
an available existing operator destination and the current operator's explicit
request/authorization. Operator image durable writes require the explicit request grammar below and
a named existing Job, logical destination and mutation mode. Tools remain absent from generic
autonomous initiative and non-Job follow-up. Jobs cannot send unsolicited images.

For Job work the runtime derives the owner from the exact live
Job/JobRun/Task/ActiveGoal binding, and checks that binding before operations and
effects. Job schemas reject a supplied cross-Job selector. A retained Job image
cannot be saved or interpreted under another Job's binding. Changing foreground
selection does not transfer ownership.

Each unguessable runtime-owned `img_…` reference binds immutable encoded bytes
and their SHA-256 `content_version` to one attention episode. The map is cleared
on completion, error, or cancellation. A reference from another episode or
runtime is rejected. Replacing a stored path after load does not change that
snapshot: interpretation and delivery consume its bytes, never reopen the path.
Identical operator acquisitions reuse the existing result without repeating I/O.

## Operator save authorization

A standalone operator command establishes one request-scoped image-save grant:

```text
ask save image {"job":"JOB7","path":"validation/camera.jpg","mode":"create"}
```

The text after `ask` must be exactly `save image ` followed by one JSON object
with exactly the string fields `job`, `path` and `mode` (at most 1,024 command
characters). `job` is an exact existing ID or case-sensitive name. `path` must
pass the ordinary contained logical-path grammar. `mode` must be `create`,
`replace` or `upsert`; it grants only that mode, never a broader write permission.
Surrounding whitespace is allowed. Duplicate JSON fields, extra fields, multiple
commands, quoted examples, surrounding prose, malformed paths, missing Jobs and
ambiguous names grant no authorization. Natural-language consent and a later
“yes” do not mint a grant; use this exact typed command through console `ask` or
operator text dialogue. Capture/load/interpret/delivery requests keep their
existing grammar.

The runtime parses and resolves the command before calling cognition, independently
of the model. The model-facing save schema is restricted to the resolved Job ID,
exact path and mode. The executor checks those fields again against the private
runtime grant and the live operator episode before calling the storage effect.
A model-supplied authorization field is not accepted. Greetings, interpretation-only
requests and send-only requests cannot save an image even after acquiring one.
Consent from prior turns, WorkingMemory, retrieved artifacts or model replies is
not authorization for the current request.

The grant permits one current-episode retained image to be saved at that exact
destination using the existing single operator effect opportunity. The image
must still be acquired through the existing camera/load tools and reference checks;
the grant does not capture automatically or call vision. The existing `create`
conflict and replacement/upsert failure semantics still apply. Nothing replays
an indeterminate write. Job-work save schemas and authority continue to use the
exact live Job/JobRun/Task/goal binding without an operator-command grant.

## Formats, storage and provenance

Pillow (`Pillow>=11.3,<13`) is a core dependency for bounded raster validation.
The decoder checks the actual JPEG/PNG format, header dimensions, single-frame
status, structural verification, and full decodability. Unsupported formats,
animated PNGs, malformed/truncated images and decompression bombs fail before
publication. Dimension/pixel ceilings are checked before decoding pixels; encoded
bytes and projected shared storage quotas are checked before invoking the decoder
on a write. Loading/listing images also checks shared quotas before decoding.
Bytes, including original encoding metadata, are preserved exactly. Nothing
resizes, recompresses or transcodes the artifact or outbound image.

| Ceiling | Value |
| --- | ---: |
| Image encoded bytes | 4 MiB |
| Width or height | 4,096 pixels |
| Total pixels | 4,194,304 |
| Runtime provenance per image | 2,048 encoded bytes |
| Published files across text and images | 128 |
| Published artifact bytes across text and images | 8 MiB |
| Contained namespace accounting | 1,024 entries, depth 8 |
| Listing page | 100 entries |

Text limits remain 16 KiB per write request, 256 KiB per text artifact, and 8,000
characters per read. Text tools reject image reads and replacement/append/upsert
of an image. Image tools reject replacement of a text artifact. An ordinary text
file named `.png` remains text; a validated PNG named `.txt` remains an image.
The namespace traversal is bounded for externally placed entries as well; an
externally over-quota or over-deep namespace fails closed. There is no model-facing
delete or repair command.

Images are ordinary artifact files in the existing derived Job Workspace layout.
Listings distinguish `kind="image"` and include logical path, actual media type,
width, height, encoded size and SHA-256 version. The console's existing
`job files JOB<n> [directory]` displays these fields; `job file` remains text-only.

Runtime saves attach one bounded JSON record to the staged inode's Linux extended
attribute `user.embodied_runtime.image`. It holds schema version, source kind,
source identity where known, original capture time (nullable), storage time,
owning Job, originating JobRun where applicable, actual media type/dimensions and
exact content version. Camera capture uses the camera adapter's original Unix
capture timestamp; retrieval and interpretation never replace it. A resaved
snapshot preserves known capture/source metadata and applicable originating run;
new storage time records the new save. Unknown source/capture time stays unknown.

This requires a Linux filesystem with `user.*` extended attributes and existing
`renameat2(RENAME_NOREPLACE)` support. No sidecar, revision history, database image
row or general provenance ledger is added. The attribute is set before file fsync
and atomic publication. Unsupported attribute storage, validation, quota or file
fsync failures leave prior bytes/provenance intact. Atomic publication moves the
new inode and its record together; a crash cannot make the old record describe
new bytes. Replacement does not retain a prior revision. Failed creates clean up
only newly created empty directories where possible; abrupt crashes may leave
safe private temporary files or empty directories.

Retrieval reads bytes and the attribute from the same no-follow regular-file
descriptor, rejects hard links, and validates the record against the actual byte
hash, dimensions, media type and Job. Missing, oversized, malformed or mismatched
records produce unknown provenance/capture metadata, including externally placed
images. Storage modification/retrieval time is never evidence of capture time.
The attribute is a runtime record under the existing local-filesystem trust
boundary, not a signature proving authenticity against an actor able to edit
runtime files. Backups must preserve extended attributes to preserve provenance;
a copy without attributes retains image bytes but has unknown provenance.

Directory fsync failure after publication reports `status="indeterminate"`,
`published=true`, `durability_confirmed=false`. The new image and matching record
may already be visible. Do not automatically replay this write or acknowledge
confirmed durability. An operator can inspect the path/version in a later episode
and decide what to do. Confirmed durability proves storage, never visual truth,
correct interpretation, or present-world state.

## Historical visual evidence and delivery

`interpret_image` passes an explicit `ImageSnapshot` with nullable capture time
through the configured `VisualPerceptionBackend`. It never manufactures a fresh
`CameraFrame` sensor timestamp. Remote instructions describe historical saved
imagery and known original capture time or unknown age. The result records
`captured_at` and `interpreted_at` separately, plus content version and fallible,
non-authoritative historical meaning. Saved-image results do not create fresh
sensor observations in WorkingMemory, fresh observation Findings, or current
sensor progress bases. `observe_scene` retains its ordinary one-current-frame
behavior and availability requirements.

Configured OpenRouter Gemini and direct OpenAI fallback use the existing ordered
chain, per-entry and total deadlines, attempt/usage accounting, zero SDK retries,
refusal policy and cancellation behavior. See [vision configuration](visual-perception.md)
and [provider validation](portable-vision-validation.md). Saved-image vision
works with `--camera none`; cognition is still required. Health/camera inspection
reports saved-image vision separately from camera capture/current-scene availability.

Delivery uses the existing operator image sink and MMS bearer store. JPEG and PNG
retain their media type on submission and GET/HEAD fetches. Existing 4-image,
12 MiB, 15-minute staging bounds and uncertain-submission/no-replay policy remain.
Provider submission acceptance is distinct from carrier delivery or handset
receipt. No vision call is made for delivery.

## Post-merge validation for Nick

These are manual deployment procedures, not claims of live validation. Capture
uses physical hardware; cognition/vision and MMS calls may cost money. Offline
tests use fake cameras, providers and a local fake MMS gateway only.

Use Python 3.13+ and your normal trusted deployment checkout:

```sh
source .venv/bin/activate
python -m pip install -e '.[openai,twilio]'
python -m unittest tests.test_workspace_images tests.test_workspace_image_cognition tests.test_workspace_image_mms -q
```

Prepare `config/mira-vision.toml` and its protected credentials using
[the existing Gemini → OpenAI setup](portable-vision-validation.md#prepare-deployment-configuration-and-credentials).
Keep `[jobs]` persistence enabled and use the same Jobs database across all
launches. For MMS, retain your existing Twilio configuration, protected credentials,
operator number, public HTTPS origin (`TWILIO_PUBLIC_MEDIA_BASE_URL`) and listener
exposure from [SMS setup](sms.md). No credential belongs in a prompt or TOML value.
Only one runtime should own the camera/database; stop an existing runtime using
your ordinary operator procedure before starting the console.

Start the console:

```sh
python main.py --config config/mira-vision.toml --console
```

At the Mira prompt, run `jobs` and choose an **existing** Job. The commands below
use `JOB7`; replace it with that exact existing ID. `validation/camera.jpg` is the
explicit destination authorized by these example requests; choose another exact
path if desired and use that same path in each request. Use `create` for the first
save. If it exists, stop and explicitly choose another path or authorize `replace`.
Do not create a Job merely to satisfy the tool.

```text
jobs
camera status
ask save image {"job":"JOB7","path":"validation/camera.jpg","mode":"create"}
job files JOB7 validation
quit
```

Verify one camera capture, one save effect, `applied`, `published=true` and
`durability_confirmed=true`, and record the listing's SHA-256 version, media type,
dimensions and size. There must be no vision submission for this request. A prose
acknowledgement alone is insufficient. An indeterminate result requires inspection
and a new explicit decision; do not repeat the save automatically.

Restart using the same database to prove persistence and camera independence:

```sh
python main.py --config config/mira-vision.toml --camera none --console
```

Then use a later, separate episode:

```text
job files JOB7 validation
ask Load JOB7 path validation/camera.jpg with workspace_load_image, then use interpret_image on that exact image_ref to describe the visible objects and uncertainty. Treat it as historical; preserve original capture time or explicitly unknown age. Do not save, send, or capture anything.
```

Verify the same content version, one load and one interpretation (two
acquisitions), no camera capture, original `captured_at`, separate `interpreted_at`,
and bounded fallible prose. `[VISION]` and the run summary should show the accepted
primary `openai-compatible`, endpoint `https://openrouter.ai`, and your configured
Gemini model. If only direct OpenAI accepts, fallback worked but Gemini has **not**
been validated. The existing fixed-image primary-only procedure can isolate that
failure. Eligible primary failures may submit the same snapshot to direct OpenAI;
refusal/cancellation must not trigger another attempt. To test controlled fallback
without a paid failing primary call, use the existing missing-primary-credential
probe from the provider validation document.

In another episode, request delivery (camera can remain disabled):

```text
ask Load JOB7 path validation/camera.jpg with workspace_load_image and send that exact image_ref using deliver_image to sms with an empty caption. I explicitly authorize MMS delivery to my configured operator number. Do not interpret, save, or capture anything.
quit
```

Verify one load and one delivery, no camera/vision request, `submission=accepted`,
a provider reference/status and `handset_receipt=unconfirmed`. Confirm receipt and
picture content on Nick's handset separately; Twilio acceptance alone is not
receipt. Check carrier/Twilio status if it does not arrive, and do not automatically
resend an uncertain submission. The outbound URL contains a temporary bearer;
keep it out of shared diagnostics. Storage stays exact even if a carrier later
transforms the delivered media.

For an explicitly authorized image Job, its work instructions can instead say:
“Capture once and create `photos/frame.jpg` in this Job's Workspace; do not
interpret or send.” Later work can load that exact path and interpret its snapshot.
Those tools have no `job` argument, use normal acquisition/effect budgets, and
cannot authorize unsolicited delivery. No existing Job is edited by this feature.
