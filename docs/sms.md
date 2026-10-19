# Twilio SMS and inbound MMS images

## Outbound camera pictures

When a camera and SMS are enabled, operator dialogue may capture one fresh JPEG and
send that exact retained frame as an MMS. Capture is a read-only acquisition and
does not itself authorize sending; delivery is a separate, single operator effect.
It does not invoke visual interpretation. Provider acceptance only proves that
Twilio accepted the API request, not that a handset received the message.

To enable outbound pictures, set optional `TWILIO_PUBLIC_MEDIA_BASE_URL` to the
trusted public HTTPS **origin** that maps
to the SMS HTTP listener (for example, `https://example.ngrok-free.app`, with no
path). The runtime never derives this origin from request headers. It stages only
runtime-retained camera or [Workspace image snapshots](workspace-images.md) at
unpredictable bearer URLs under `/outbound-media/`. Twilio `GET` and `HEAD` fetches
serve exact JPEG or PNG bytes with `image/jpeg` or `image/png`, respectively.
Loading and sending a saved image requires no camera or vision request.

Staging is volatile and bounded for small hardware: at most four images and 12 MiB
total, each no larger than 4 MiB, for 15 minutes. Capacity is rejected rather than
evicted. Failed provider submissions remove their staged image; accepted or
uncertain submissions remain for the fetch window. A periodic reaper removes
expired bytes even when Twilio makes no further request. Shutdown removes all bytes
and nothing is replayed after restart. The aiohttp access log is disabled because
request paths contain bearer credentials; sanitized method/status records remain.

Physical acceptance procedure:

1. Configure Twilio and the public HTTPS origin, then expose the existing listener
   through ngrok.
2. From SMS, voice, and console in separate turns, ask: “Send me a picture of what
   you're looking at.”
3. Verify one MMS arrives at the configured operator number and the normal reply
   remains on the originating channel.
4. Test camera contention and a missing public-media setting produce honest
   failures and no MMS.

SMS is a provider transport mapped to `remote_text`; it is not a new cognition
authority. A valid Twilio webhook signature authenticates the request received
from Twilio, not the human holding the originating telephone. P1's transport
identity policy classifies an exact match with the one configured operator number
as `operator`; every other sender is ignored before cognition.

Accepted messages enter the existing bounded operator cognition path as
`remote_text`, `dialogue`, `bounded_turn`, `operator`, with a response expected.
The configured `InteractionEnvironment` is unchanged. Replies are direct dialogue
responses only: SMS is not an autonomous notification or general delivery route.

## First-light setup

For a managed Pi deployment, use the wizard's [hardened ngrok ingress](ngrok-ingress.md):
cloud Twilio signature verification, an exact route/method allowlist, and separate
webhook/media limits. The ad-hoc first-light tunnel below does not install that
policy and should not be used as the unattended deployment workflow.

1. Install optional dependencies: `python -m pip install -e '.[twilio]'`.
2. Create, protect, and source `~/.config/embodied-runtime/twilio.env` exactly as
   described in [secrets](secrets.md).
3. Start the runtime with SMS opted in, for example:
   `python main.py --config config/mira-agentic.toml --sms`.
4. In another shell on the same host run `ngrok http 8080`.
5. Append `/sms` to ngrok's HTTPS forwarding host. Set that exact URL as
   `TWILIO_WEBHOOK_URL` and in Twilio Console under **Phone Numbers → selected
   number → Messaging → A MESSAGE COMES IN → Webhook → HTTP POST**.
6. Send a text from the exact configured operator number.

Signature validation always uses the exact configured `TWILIO_WEBHOOK_URL`, never
the local aiohttp URL or forwarded headers. When an ngrok URL changes, update the
Twilio Console, `TWILIO_WEBHOOK_URL` in the protected file, and the sourced shell
environment. Managed deployment and edge acceptance are described in
[startup](startup.md) and [ngrok ingress hardening](ngrok-ingress.md).

## Bounded behavior

The service owns one aiohttp listener and one sequential worker. It acknowledges
accepted webhooks immediately with empty TwiML. Its FIFO holds 16 accepted messages;
a full queue returns 503 without deduplicating the rejected message. A bounded
256-entry FIFO cache suppresses accepted `MessageSid` duplicates. The
queue state is volatile. Durable conversation-media ingestion state separately
tracks partial intake and claimed/completed analysis. Partial retries reuse saved
attachments and recover missing indices, including after restart; completed or
indeterminate analyses are not replayed while their bounded state records remain.
A crash before the worker starts can still lose queued work.

Plain SMS behavior is unchanged. MMS accepts up to four JPEG, PNG or WebP images
by default, with or without a caption, preserving attachment index order. Count
is configurable through `[vision] max_images`; oversized counts return HTTP 413.
The signed webhook queues only Twilio media references and returns immediately.
The sequential worker authenticates only to validated Twilio Message Media URLs;
the existing restricted CDN redirect handling is unchanged. Declared and streamed
sizes are independently bounded to 4 MiB per image, with aggregate byte/pixel
limits and raster validation. Unsupported MIME types return HTTP 415.

Accepted image bytes are automatically retained under the configurable
[conversation-media policy](conversation-images.md), separately from Job workspaces.
Dialogue stores lightweight references, and selected pixels can be reanalyzed after
restart. Receipt times never claim capture times. Retention grants no workspace
save or image-delivery authority. Failed/disabled persistence is reported and
current-turn analysis can continue. The attachment remains operator-supplied input,
not evidence of a camera observation. Mira replies by ordinary text SMS.

Replies longer than 1,600
characters are not truncated or split; the runtime makes at most one send using a
short deterministic explanation. Empty responses are not sent. Cognition failure
gets no SMS response. Provider sending runs off the asyncio event loop, has one
attempt, and has no retry queue, delivery callback, or receipt polling. When conversation
history is enabled, request-correlated attempts and provider acceptance/rejection or
uncertainty are retained as described in [conversation continuity](conversation-history.md).
Provider acceptance does not establish handset delivery; no raw provider payload or
message body is copied into the delivery ledger. Explicit report submissions retain
`kind: report` and the exact JobRun or Workspace path/content-version source from the
acquired snapshot, independently of provider acceptance or uncertainty.

Raw runtime logs intentionally contain the text of accepted configured-operator
messages and successfully sent replies, notifications, and operator deliveries for
development observability. Text uses an escaped, single-record `text=` representation.
Bodies from rejected requests and external participants are not logged. Credentials,
full phone numbers, headers, forms, media URLs, and the configured public URL remain
excluded. Effective diagnostics expose structural listener settings and booleans for
private-value availability, never those values. Cognition-facing
`inspect_run_history` withholds content-bearing `text=` lines before matching, so the
raw logging does not create a conversation-history retrieval path; run history remains
operational evidence rather than a conversation replay or archive.
