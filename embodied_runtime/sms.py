"""Narrow Twilio transport for inbound, bounded operator SMS dialogue."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import logging
import os
import re
import secrets
from time import monotonic
from typing import Any, Protocol
from urllib.parse import urlsplit

from embodied_runtime.attachments import (
    ImageAttachment, MAX_INTERACTION_IMAGE_BYTES,
    SUPPORTED_INTERACTION_IMAGE_TYPES,
)

from embodied_runtime.interaction import (
    InteractionCadence, InteractionChannel, InteractionContext,
    InteractionInitiator, InteractionMode, OperatorMessage, OperatorMessageSink,
)
from embodied_runtime.outbound_images import (
    ImageDeliveryEvidence, MAX_OUTBOUND_IMAGE_BYTES, RetainedImage,
)

LOGGER = logging.getLogger(__name__)
TWILIO_HTTP_LOGGER = "twilio.http_client"
SMS_INBOX_SIZE = 16
SMS_DEDUPE_SIZE = 256
MAX_SMS_BODY_CHARS = 1600
EMPTY_TWIML = "<Response></Response>"
TOO_LONG_REPLY = (
    "I generated a reply that was too long for SMS. Please ask me for a shorter version."
)
MEDIA_FAILURE_REPLY = (
    "I received the image, but I couldn't load it safely. Please try sending it again."
)
MEDIA_DOWNLOAD_TIMEOUT_SECONDS = 20
_E164 = re.compile(r"\+[1-9][0-9]{1,14}\Z")
_REQUIRED_ENV = (
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER",
    "MIRA_SMS_OPERATOR_NUMBER", "TWILIO_WEBHOOK_URL",
)
OUTBOUND_MEDIA_PATH_PREFIX = "/outbound-media/"
OUTBOUND_MEDIA_TTL_SECONDS = 15 * 60
OUTBOUND_MEDIA_MAX_ITEMS = 4
OUTBOUND_MEDIA_MAX_TOTAL_BYTES = 12 * 1024 * 1024


class SmsConfigurationError(ValueError):
    """An enabled SMS transport is missing safe, valid configuration."""


class SmsProviderUnavailableError(RuntimeError):
    """The optional Twilio transport dependencies are unavailable."""


@dataclass(frozen=True, slots=True)
class TwilioSmsSettings:
    account_sid: str
    auth_token: str
    twilio_number: str
    operator_number: str
    webhook_url: str
    bind_host: str
    bind_port: int
    webhook_path: str
    public_media_base_url: str | None = None

    @classmethod
    def from_environment(
        cls, *, bind_host: str, bind_port: int, webhook_path: str,
        environ: Mapping[str, str] | None = None,
    ) -> TwilioSmsSettings:
        values = os.environ if environ is None else environ
        missing = [name for name in _REQUIRED_ENV if not values.get(name, "").strip()]
        if missing:
            raise SmsConfigurationError(
                "enabled Twilio SMS requires environment variables: " + ", ".join(missing)
            )
        twilio_number = values["TWILIO_PHONE_NUMBER"]
        operator_number = values["MIRA_SMS_OPERATOR_NUMBER"]
        if not _E164.fullmatch(twilio_number) or not _E164.fullmatch(operator_number):
            raise SmsConfigurationError("Twilio and operator SMS numbers must be exact E.164 values")
        webhook_url = values["TWILIO_WEBHOOK_URL"]
        parsed = urlsplit(webhook_url)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.path != webhook_path
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise SmsConfigurationError(
                "TWILIO_WEBHOOK_URL must be HTTPS with the configured webhook path "
                "and no credentials, query, or fragment"
            )
        public_media_base_url = values.get("TWILIO_PUBLIC_MEDIA_BASE_URL", "").strip() or None
        if public_media_base_url is not None:
            public_media_base_url = public_media_base_url.rstrip("/")
            media_parsed = urlsplit(public_media_base_url)
            if (media_parsed.scheme != "https" or not media_parsed.netloc
                    or media_parsed.path or media_parsed.query or media_parsed.fragment
                    or media_parsed.username or media_parsed.password):
                raise SmsConfigurationError(
                    "TWILIO_PUBLIC_MEDIA_BASE_URL must be an HTTPS origin without path, credentials, query, or fragment"
                )
        return cls(
            values["TWILIO_ACCOUNT_SID"], values["TWILIO_AUTH_TOKEN"],
            twilio_number, operator_number, webhook_url, bind_host, bind_port,
            webhook_path, public_media_base_url,
        )


@dataclass(frozen=True, slots=True)
class TwilioMediaReference:
    url: str
    declared_media_type: str


@dataclass(frozen=True, slots=True)
class SmsInboundMessage:
    message_sid: str
    sender: str
    body: str
    media: TwilioMediaReference | None = None


class MediaDownloadError(RuntimeError):
    """A Twilio media resource failed a bounded safety check."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MediaDownloader(Protocol):
    async def download(self, reference: TwilioMediaReference) -> ImageAttachment: ...


class TwilioMediaDownloader:
    """Authenticated, bounded acquisition of only Twilio Message Media resources."""

    _PATH = re.compile(
        r"/2010-04-01/Accounts/([^/]+)/Messages/([^/]+)/Media/([^/.]+)\Z"
    )

    def __init__(self, settings: TwilioSmsSettings) -> None:
        self._account_sid = settings.account_sid
        self._auth_token = settings.auth_token

    def _validate_url(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise MediaDownloadError("invalid_media_url") from error
        match = self._PATH.fullmatch(parsed.path)
        if (
            parsed.scheme != "https" or parsed.hostname != "api.twilio.com"
            or port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment or match is None
            or match.group(1) != self._account_sid
        ):
            raise MediaDownloadError("invalid_media_url")

    @staticmethod
    def _validate_redirect_url(url: str) -> None:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (TypeError, ValueError) as error:
            raise MediaDownloadError("invalid_media_redirect") from error
        if (
            parsed.scheme != "https" or parsed.hostname != "mms.twiliocdn.com"
            or port not in (None, 443) or parsed.username is not None
            or parsed.password is not None or parsed.fragment
        ):
            raise MediaDownloadError("invalid_media_redirect")

    @staticmethod
    async def _read_image_response(response: Any, declared: str) -> bytes:
        if response.status == 401 or response.status == 403:
            raise MediaDownloadError("media_auth_failed")
        if response.status == 404:
            raise MediaDownloadError("media_not_found")
        if response.status != 200:
            raise MediaDownloadError("media_download_failed")
        length = response.content_length
        if length is not None and length > MAX_INTERACTION_IMAGE_BYTES:
            raise MediaDownloadError("media_too_large")
        actual = _normalized_media_type(response.headers.get("Content-Type", ""))
        if actual != declared:
            raise MediaDownloadError("media_type_mismatch")
        chunks = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            if len(chunks) + len(chunk) > MAX_INTERACTION_IMAGE_BYTES:
                raise MediaDownloadError("media_too_large")
            chunks.extend(chunk)
        return bytes(chunks)

    async def download(self, reference: TwilioMediaReference) -> ImageAttachment:
        declared = _normalized_media_type(reference.declared_media_type)
        if declared not in SUPPORTED_INTERACTION_IMAGE_TYPES:
            raise MediaDownloadError("unsupported_media_type")
        self._validate_url(reference.url)
        try:
            from aiohttp import ClientSession, ClientTimeout, encode_basic_auth
            timeout = ClientTimeout(
                total=MEDIA_DOWNLOAD_TIMEOUT_SECONDS, connect=5, sock_read=10,
            )
            async with ClientSession(timeout=timeout) as session:
                async with session.get(
                    reference.url,
                    headers={"Authorization": encode_basic_auth(
                        self._account_sid, self._auth_token,
                    )},
                    allow_redirects=False,
                ) as response:
                    if response.status == 200:
                        data = await self._read_image_response(response, declared)
                    elif response.status == 307:
                        location = response.headers.get("Location")
                        if location is None:
                            raise MediaDownloadError("invalid_media_redirect")
                        self._validate_redirect_url(location)
                        async with session.get(
                            location, allow_redirects=False,
                        ) as final_response:
                            if 300 <= final_response.status < 400:
                                raise MediaDownloadError("invalid_media_redirect")
                            data = await self._read_image_response(final_response, declared)
                    else:
                        await self._read_image_response(response, declared)
        except MediaDownloadError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as error:
            raise MediaDownloadError("media_download_timeout") from error
        except Exception as error:
            raise MediaDownloadError("media_download_failed") from error
        if not _matches_magic(declared, data):
            raise MediaDownloadError("media_type_mismatch")
        return ImageAttachment(declared, data)


def _normalized_media_type(value: str) -> str:
    return value.partition(";")[0].strip().lower()


def _matches_magic(media_type: str, data: bytes) -> bool:
    if media_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


class SmsSender(Protocol):
    def send(self, *, from_: str, to: str, body: str, **kwargs: Any) -> Any: ...


@dataclass(slots=True)
class _StagedMedia:
    data: bytes
    media_type: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class _Acceptance:
    status: int
    reason: str


class TwilioSmsGateway:
    """Twilio SDK boundary for request validation and synchronous sending."""

    def __init__(self, settings: TwilioSmsSettings) -> None:
        # twilio-python emits request URLs, headers, and response headers at
        # INFO on this dedicated logger.  Keep that provider transport detail
        # out of runtime logs without changing application or other HTTP logs.
        twilio_http_logger = logging.getLogger(TWILIO_HTTP_LOGGER)
        twilio_http_logger.setLevel(logging.WARNING)
        try:
            from twilio.request_validator import RequestValidator
            from twilio.rest import Client
        except ImportError as error:
            raise SmsProviderUnavailableError(
                "Twilio SMS requires the optional 'twilio' dependencies"
            ) from error
        self._validator = RequestValidator(settings.auth_token)
        self._client = Client(settings.account_sid, settings.auth_token)
        # Preserve the boundary even if SDK client initialization adjusted its
        # own logger level.
        twilio_http_logger.setLevel(logging.WARNING)

    def validate(
        self, url: str, form: Any, signature: str,
    ) -> bool:
        return bool(signature) and self._validator.validate(url, form, signature)

    def send(
        self, *, from_: str, to: str, body: str,
        media_url: list[str] | None = None,
    ) -> Any:
        arguments: dict[str, Any] = {"from_": from_, "to": to, "body": body}
        if media_url is not None:
            arguments["media_url"] = media_url
        return self._client.messages.create(**arguments)


class TwilioSmsService(OperatorMessageSink):
    """Own one aiohttp endpoint, bounded FIFO, dedupe cache, and worker."""

    def __init__(
        self, settings: TwilioSmsSettings,
        request_cognition: Callable[..., Awaitable[str]],
        *, gateway: SmsSender | Any | None = None,
        media_downloader: MediaDownloader | None = None,
        inbox_size: int = SMS_INBOX_SIZE, dedupe_size: int = SMS_DEDUPE_SIZE,
    ) -> None:
        self.settings = settings
        self._request_cognition = request_cognition
        self._gateway = gateway
        self._media_downloader = media_downloader or TwilioMediaDownloader(settings)
        self._inbox: asyncio.Queue[SmsInboundMessage] = asyncio.Queue(inbox_size)
        self._dedupe_size = dedupe_size
        self._accepted: set[str] = set()
        self._accepted_fifo: deque[str] = deque()
        self._runner: Any = None
        self._worker: asyncio.Task[None] | None = None
        self._accepting = False
        self._staged: dict[str, _StagedMedia] = {}

    @property
    def channel(self) -> InteractionChannel:
        return InteractionChannel.REMOTE_TEXT

    @property
    def image_delivery_available(self) -> bool:
        return bool(self.settings.public_media_base_url) and self._accepting

    def _purge_expired_media(self) -> None:
        now = monotonic()
        self._staged = {key: item for key, item in self._staged.items()
                        if item.expires_at > now}

    def _stage_image(self, image: RetainedImage) -> str:
        self._purge_expired_media()
        if image.media_type != "image/jpeg" or not image.data.startswith(b"\xff\xd8\xff"):
            raise ValueError("outbound media must be a valid JPEG")
        if not image.data or len(image.data) > MAX_OUTBOUND_IMAGE_BYTES:
            raise ValueError("outbound image exceeds the byte limit")
        if (len(self._staged) >= OUTBOUND_MEDIA_MAX_ITEMS
                or sum(len(item.data) for item in self._staged.values()) + len(image.data)
                > OUTBOUND_MEDIA_MAX_TOTAL_BYTES):
            raise RuntimeError("outbound media staging capacity is full")
        token = secrets.token_urlsafe(32)
        self._staged[token] = _StagedMedia(
            image.data, image.media_type, monotonic() + OUTBOUND_MEDIA_TTL_SECONDS)
        return token

    async def deliver_image(
        self, image: RetainedImage, caption: str | None,
    ) -> ImageDeliveryEvidence:
        """Stage and submit one exact retained image to the configured operator."""
        if not self.image_delivery_available or self._gateway is None:
            raise RuntimeError("outbound MMS media is not configured or ready")
        token = self._stage_image(image)
        media_url = f"{self.settings.public_media_base_url}{OUTBOUND_MEDIA_PATH_PREFIX}{token}"
        try:
            result = await asyncio.to_thread(
                self._gateway.send, from_=self.settings.twilio_number,
                to=self.settings.operator_number, body=caption or "", media_url=[media_url],
            )
        except (asyncio.TimeoutError, TimeoutError):
            # Submission may have reached Twilio. Retain media and never retry.
            return ImageDeliveryEvidence("uncertain")
        except Exception:
            self._staged.pop(token, None)
            raise
        status = str(getattr(result, "status", "accepted"))
        sid = getattr(result, "sid", None)
        return ImageDeliveryEvidence("accepted", status, str(sid) if sid else None)

    async def deliver(self, message: OperatorMessage) -> None:
        """Deliver one bounded runtime message to the configured operator only."""
        interaction = message.interaction
        notification = (
            interaction.channel == self.channel
            and interaction.mode == InteractionMode.NOTIFICATION
            and interaction.initiator == InteractionInitiator.RUNTIME
            and interaction.response_expected is False
        )
        delivery = (
            interaction.channel == self.channel
            and interaction.mode == InteractionMode.DELIVERY
            and interaction.initiator == InteractionInitiator.OPERATOR
            and interaction.response_expected is False
        )
        if not (notification or delivery):
            raise ValueError("SMS accepts only valid operator notifications or deliveries")
        if not self._accepting or self._gateway is None:
            raise RuntimeError("SMS service is not ready")
        body = message.text.strip()
        if not body:
            raise ValueError("SMS operator message must be non-empty")
        if len(body) > MAX_SMS_BODY_CHARS:
            raise ValueError(
                f"SMS operator message must be at most {MAX_SMS_BODY_CHARS} characters"
            )
        try:
            await asyncio.to_thread(
                self._gateway.send, from_=self.settings.twilio_number,
                to=self.settings.operator_number, body=body,
            )
        except Exception:
            LOGGER.error("[SMS] operator_delivery status=failed reason=provider_error")
            raise
        LOGGER.info("[SMS] operator_delivery chars=%s text=%r status=sent",
                    len(body), body)

    @property
    def diagnostics(self) -> dict[str, object]:
        return {
            "enabled": True, "backend": "twilio",
            "bind_host": self.settings.bind_host, "bind_port": self.settings.bind_port,
            "webhook_path": self.settings.webhook_path,
            "credentials_available": True, "twilio_number_configured": True,
            "operator_number_configured": True, "webhook_url_configured": True,
            "outbound_media_configured": bool(self.settings.public_media_base_url),
        }

    async def start(self) -> None:
        if self._runner is not None:
            return
        try:
            from aiohttp import web
        except ImportError as error:
            raise SmsProviderUnavailableError(
                "Twilio SMS requires the optional 'aiohttp' dependency"
            ) from error
        if self._gateway is None:
            self._gateway = TwilioSmsGateway(self.settings)
        app = web.Application()
        app.router.add_post(self.settings.webhook_path, self._handle_webhook)
        app.router.add_route("GET", OUTBOUND_MEDIA_PATH_PREFIX + "{token}", self._handle_outbound_media)
        app.router.add_route("HEAD", OUTBOUND_MEDIA_PATH_PREFIX + "{token}", self._handle_outbound_media)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self.settings.bind_host, self.settings.bind_port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        self._accepting = True
        self._worker = asyncio.create_task(self._run_worker(), name="sms-remote-text")
        LOGGER.info("[SMS] backend=twilio bind=%s:%s path=%s status=ready",
                    self.settings.bind_host, self.settings.bind_port,
                    self.settings.webhook_path)

    async def stop(self) -> None:
        # This transition precedes listener cleanup so handlers already in flight
        # cannot commit newly discarded work while shutdown waits for them.
        self._accepting = False
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if runner is not None or worker is not None:
            LOGGER.info("[SMS] status=stopped")
        self._staged.clear()

    async def _handle_outbound_media(self, request: Any) -> Any:
        from aiohttp import web
        self._purge_expired_media()
        item = self._staged.get(request.match_info.get("token", ""))
        if item is None:
            return web.Response(status=404)
        return web.Response(
            body=b"" if request.method == "HEAD" else item.data,
            content_type=item.media_type,
            headers={"Cache-Control": "private, max-age=60", "X-Content-Type-Options": "nosniff"},
        )

    async def _handle_webhook(self, request: Any) -> Any:
        from aiohttp import web
        if not self._accepting:
            return web.Response(status=503)
        try:
            form_data = await request.post()
        except Exception:
            LOGGER.info("[SMS] inbound status=rejected reason=malformed_form")
            return web.Response(status=400)
        signature = request.headers.get("X-Twilio-Signature", "")
        if not self._gateway.validate(
            self.settings.webhook_url, form_data, signature
        ):
            LOGGER.info("[SMS] inbound status=rejected reason=invalid_signature")
            return web.Response(status=403)
        # Only after validating the complete signed form do we select understood fields.
        required = ("MessageSid", "AccountSid", "From", "To", "Body", "NumMedia")
        form: dict[str, str] = {}
        for key in required:
            values = form_data.getall(key, [])
            if not values:
                LOGGER.info("[SMS] inbound status=rejected reason=missing_fields")
                return web.Response(status=400)
            if len(values) != 1:
                LOGGER.info("[SMS] inbound status=rejected reason=ambiguous_fields")
                return web.Response(status=400)
            form[key] = str(values[0])
        # Classify the authenticated transport envelope before projecting any
        # media-specific fields. External participants never expose a media
        # reference to this runtime, even transiently.
        if (form["AccountSid"] != self.settings.account_sid
                or form["To"] != self.settings.twilio_number):
            LOGGER.info("[SMS] inbound status=rejected reason=account_or_destination")
            return web.Response(status=403)
        try:
            media_count = int(form["NumMedia"])
        except ValueError:
            return web.Response(status=400)
        if media_count < 0:
            return web.Response(status=400)
        if form["From"] != self.settings.operator_number:
            LOGGER.info("[SMS] inbound status=ignored reason=external_participant")
            return self._twiml(web)
        if media_count == 1:
            for key in ("MediaUrl0", "MediaContentType0"):
                values = form_data.getall(key, [])
                if not values:
                    LOGGER.info("[SMS] inbound status=rejected reason=missing_fields")
                    return web.Response(status=400)
                if len(values) != 1:
                    LOGGER.info("[SMS] inbound status=rejected reason=ambiguous_fields")
                    return web.Response(status=400)
                form[key] = str(values[0])
        result = self._accept_validated_form(form)
        if result.status == 200:
            return self._twiml(web)
        return web.Response(status=result.status)

    def _accept_validated_form(self, form: Mapping[str, str]) -> _Acceptance:
        """Apply the provider-independent bounded acceptance state machine."""
        if not self._accepting:
            return _Acceptance(503, "stopping")
        required = ("MessageSid", "AccountSid", "From", "To", "Body", "NumMedia")
        if any(name not in form for name in required):
            LOGGER.info("[SMS] inbound status=rejected reason=missing_fields")
            return _Acceptance(400, "missing_fields")
        if form["AccountSid"] != self.settings.account_sid or form["To"] != self.settings.twilio_number:
            LOGGER.info("[SMS] inbound status=rejected reason=account_or_destination")
            return _Acceptance(403, "account_or_destination")
        try:
            media_count = int(form["NumMedia"])
        except ValueError:
            return _Acceptance(400, "invalid_media_count")
        if media_count < 0:
            return _Acceptance(400, "invalid_media_count")
        if form["From"] != self.settings.operator_number:
            LOGGER.info("[SMS] inbound status=ignored reason=external_participant")
            return _Acceptance(200, "external_participant")
        if media_count > 1:
            LOGGER.info("[SMS] inbound status=ignored reason=multiple_media_unsupported")
            return _Acceptance(200, "media_unsupported")
        if media_count == 1 and any(
            not form.get(name) for name in ("MediaUrl0", "MediaContentType0")
        ):
            return _Acceptance(400, "missing_fields")
        sid = form["MessageSid"]
        if sid in self._accepted:
            LOGGER.info("[SMS] inbound message_sid=%s status=duplicate", sid)
            return _Acceptance(200, "duplicate")
        media = None if media_count == 0 else TwilioMediaReference(
            form["MediaUrl0"], form["MediaContentType0"]
        )
        message = SmsInboundMessage(sid, form["From"], form["Body"], media)
        # Recheck at the commit boundary: request parsing/signature validation may
        # have yielded while stop() closed acceptance.
        if not self._accepting:
            return _Acceptance(503, "stopping")
        try:
            self._inbox.put_nowait(message)
        except asyncio.QueueFull:
            LOGGER.info("[SMS] inbound status=rejected reason=queue_full")
            return _Acceptance(503, "queue_full")
        self._remember(sid)
        LOGGER.info(
            "[SMS] inbound message_sid=%s media=%s chars=%s text=%r "
            "status=accepted sender=operator",
            sid, media_count, len(message.body), message.body,
        )
        return _Acceptance(200, "accepted")

    @staticmethod
    def _twiml(web: Any) -> Any:
        return web.Response(text=EMPTY_TWIML, content_type="application/xml")

    def _remember(self, sid: str) -> None:
        self._accepted.add(sid)
        self._accepted_fifo.append(sid)
        while len(self._accepted_fifo) > self._dedupe_size:
            self._accepted.discard(self._accepted_fifo.popleft())

    async def _run_worker(self) -> None:
        while True:
            message = await self._inbox.get()
            try:
                if not self._accepting:
                    continue
                interaction = InteractionContext(
                    channel=InteractionChannel.REMOTE_TEXT,
                    mode=InteractionMode.DIALOGUE,
                    cadence=InteractionCadence.BOUNDED_TURN,
                    initiator=InteractionInitiator.OPERATOR,
                    response_expected=True,
                )
                attachments = ()
                if message.media is not None:
                    try:
                        image = await self._media_downloader.download(message.media)
                    except asyncio.CancelledError:
                        raise
                    except MediaDownloadError as error:
                        LOGGER.info("[SMS] media message_sid=%s status=failed reason=%s",
                                    message.message_sid, error.reason)
                        await self._send_reply(message, MEDIA_FAILURE_REPLY)
                        continue
                    except Exception:
                        LOGGER.info("[SMS] media message_sid=%s status=failed "
                                    "reason=media_download_failed", message.message_sid)
                        await self._send_reply(message, MEDIA_FAILURE_REPLY)
                        continue
                    attachments = (image,)
                    LOGGER.info("[SMS] media message_sid=%s type=%s bytes=%s status=loaded",
                                message.message_sid, image.media_type, len(image.data))
                try:
                    arguments = {"interaction": interaction}
                    if attachments:
                        arguments["image_attachments"] = attachments
                    response = await self._request_cognition(message.body, **arguments)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.error("[SMS] cognition status=failed")
                    continue
                body = response.strip()
                if not body:
                    LOGGER.info("[SMS] reply status=failed reason=empty")
                    continue
                if len(body) > MAX_SMS_BODY_CHARS:
                    LOGGER.info("[SMS] reply status=failed reason=too_long")
                    body = TOO_LONG_REPLY
                await self._send_reply(message, body)
            finally:
                self._inbox.task_done()

    async def _send_reply(self, message: SmsInboundMessage, body: str) -> None:
        try:
            await asyncio.to_thread(
                self._gateway.send, from_=self.settings.twilio_number,
                to=message.sender, body=body,
            )
        except Exception:
            LOGGER.error("[SMS] reply status=failed reason=provider_error")
        else:
            LOGGER.info("[SMS] reply message_sid=%s chars=%s text=%r status=sent",
                        message.message_sid, len(body), body)
