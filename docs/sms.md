# Twilio SMS and inbound MMS images

## Outbound camera pictures

When a camera and SMS are enabled, operator dialogue may capture one fresh JPEG and
send that exact retained frame as an MMS. Capture is a read-only acquisition and
does not itself authorize sending; delivery is a separate, single operator effect.
It does not invoke visual interpretation. Provider acceptance only proves that
Twilio accepted the API request, not that a handset received the message.

Set `TWILIO_PUBLIC_MEDIA_BASE_URL` to the trusted public HTTPS **origin** that maps
to the SMS HTTP listener (for example, `https://example.ngrok-free.app`, with no
path). The runtime never derives this origin from request headers. It stages only
runtime-captured JPEGs at unpredictable bearer URLs under `/outbound-media/` and
supports Twilio's `GET` and `HEAD` fetches with `image/jpeg`.

Staging is volatile and bounded for small hardware: at most four images and 12 MiB
total, each no larger than 4 MiB, for 15 minutes. Capacity is rejected rather than
evicted. Failed provider submissions remove their staged image; accepted or
uncertain submissions remain for the fetch window. Shutdown removes all bytes and
nothing is replayed after restart.

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
environment. ngrok is development/acceptance exposure, not production architecture.

## Bounded behavior

The service owns one aiohttp listener and one sequential worker. It acknowledges
accepted webhooks immediately with empty TwiML. Its FIFO holds 16 accepted messages;
a full queue returns 503 without deduplicating the rejected message. A bounded
256-entry FIFO cache suppresses accepted `MessageSid` duplicates. Both queue and
deduplication state are volatile: a restart forgets deduplication, and a crash after
acknowledgment can lose queued work.

Plain SMS behavior is unchanged. P2 also accepts exactly one JPEG, PNG, or WebP
image, with or without a caption. The signed webhook queues only its Twilio media
reference and returns immediately. The sequential worker authenticates to a
narrowly validated `https://api.twilio.com` Message Media resource using the
configured Account SID and Auth Token; redirects are disabled. Declared and
streamed sizes are independently bounded to 4 MiB, and the signed type, HTTP type,
and JPEG/PNG/WebP magic bytes must agree. Other or multiple media do not enter
cognition.

The bytes exist only for the current request and are never written to WorkingMemory,
persistent memory, Job Workspaces, or run-history content. The attachment is
operator-supplied interaction input, not a camera observation, and consumes no
scene acquisition. Mira replies by ordinary text SMS; outbound MMS is not
implemented.

Replies longer than 1,600
characters are not truncated or split; the runtime makes at most one send using a
short deterministic explanation. Empty responses are not sent. Cognition failure
gets no SMS response. Provider sending runs off the asyncio event loop, has one
attempt, and has no retry queue, delivery callback, or receipt persistence.

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
