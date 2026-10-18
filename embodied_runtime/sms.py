"""Narrow Twilio transport for inbound, bounded operator SMS dialogue."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import os
import re
import secrets
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

from embodied_runtime.conversation_history.continuity import delivery_attempt
from embodied_runtime.images import ImageLimits
from embodied_runtime.conversation_media import MediaUnavailableError
from embodied_runtime.attachments import (
    ImageAttachment, MAX_INTERACTION_IMAGE_BYTES,
    SUPPORTED_INTERACTION_IMAGE_TYPES,
)

from embodied_runtime.interaction import (
    InteractionCadence, InteractionChannel, InteractionContext,
    InteractionInitiator, InteractionMode, OperatorMessage, OperatorMessageSink,
)
from embodied_runtime.outbound_images import (
    ImageDeliveryEvidence, MAX_OUTBOUND_IMAGE_BYTES, OutboundImage,
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
OUTBOUND_MEDIA_TTL_SECONDS = 15 * 60
OUTBOUND_MEDIA_MAX_ITEMS = 4
OUTBOUND_MEDIA_MAX_BYTES = 12 * 1024 * 1024
OUTBOUND_MEDIA_PATH = "/outbound-media/"
OUTBOUND_MEDIA_REAP_INTERVAL_SECONDS = 60


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
        media_base = values.get("TWILIO_PUBLIC_MEDIA_BASE_URL", "").strip() or None
        if media_base is not None:
            media_url = urlsplit(media_base)
            if (media_url.scheme != "https" or not media_url.netloc or media_url.path not in ("", "/")
                    or media_url.query or media_url.fragment or media_url.username
                    or media_url.password):
                raise SmsConfigurationError(
                    "TWILIO_PUBLIC_MEDIA_BASE_URL must be an HTTPS origin without a path"
                )
            media_base = media_base.rstrip("/")
        return cls(
            values["TWILIO_ACCOUNT_SID"], values["TWILIO_AUTH_TOKEN"],
            twilio_number, operator_number, webhook_url, bind_host, bind_port,
            webhook_path, media_base,
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
    media_items: tuple[TwilioMediaReference, ...] = ()
    received_at: datetime | None = None


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
        try:
            image = ImageAttachment(declared, data)
            image.raster_metadata()
        except Exception as error:
            raise MediaDownloadError("invalid_raster_image") from error
        return image


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
    def send(self, *, from_: str, to: str, body: str) -> Any: ...


@dataclass(slots=True)
class _StagedImage:
    data: bytes
    expires_at: float
    media_type: str = "image/jpeg"


class OutboundMediaStore:
    """Small in-memory bearer store; it never exposes paths or directory listings."""

    def __init__(self, *, ttl_seconds: float = OUTBOUND_MEDIA_TTL_SECONDS,
                 max_items: int = OUTBOUND_MEDIA_MAX_ITEMS,
                 max_bytes: int = OUTBOUND_MEDIA_MAX_BYTES,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._clock = clock
        self._items: dict[str, _StagedImage] = {}

    def expire(self) -> None:
        now = self._clock()
        for token in tuple(self._items):
            if self._items[token].expires_at <= now:
                del self._items[token]

    def stage(self, data: bytes, media_type: str = "image/jpeg") -> str:
        self.expire()
        if media_type not in ("image/jpeg", "image/png"):
            raise ValueError("unsupported outbound media type")
        if (not data or len(data) > MAX_OUTBOUND_IMAGE_BYTES
                or len(self._items) >= self.max_items
                or sum(len(item.data) for item in self._items.values()) + len(data) > self.max_bytes):
            raise RuntimeError("outbound media staging capacity unavailable")
        token = secrets.token_urlsafe(32)
        self._items[token] = _StagedImage(bytes(data), self._clock() + self.ttl_seconds, media_type)
        return token

    def get(self, token: str) -> bytes | None:
        self.expire()
        item = self._items.get(token)
        return None if item is None else item.data

    def get_image(self, token: str) -> OutboundImage | None:
        self.expire()
        item = self._items.get(token)
        return OutboundImage(item.data, item.media_type, "") if item else None

    def remove(self, token: str) -> None:
        self._items.pop(token, None)

    def clear(self) -> None:
        self._items.clear()

    @property
    def retained_count(self) -> int:
        self.expire()
        return len(self._items)


def _provider_failure_is_rejection(error: BaseException) -> bool:
    """Classify only explicit Twilio client-error responses as rejection.

    Connection and read failures are ambiguous because the POST may have reached
    Twilio.  Avoid importing an optional SDK merely to classify its exception.
    """
    status = getattr(error, "status", None)
    # A 4xx response establishes that Twilio rejected this request. A 5xx
    # response (including gateway timeout) cannot establish whether an upstream
    # accepted it before the failure became visible to this client.
    return type(status) is int and 400 <= status <= 499


def _bounded_provider_value(value: object, limit: int) -> str | None:
    if type(value) is not str or not value or len(value) > limit:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


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

    def send(self, *, from_: str, to: str, body: str) -> Any:
        return self._client.messages.create(from_=from_, to=to, body=body)

    def send_media(self, *, from_: str, to: str, body: str,
                   media_url: str) -> Any:
        return self._client.messages.create(
            from_=from_, to=to, body=body or None, media_url=[media_url]
        )


class TwilioSmsService(OperatorMessageSink):
    """Own one aiohttp endpoint, bounded FIFO, dedupe cache, and worker."""

    def __init__(
        self, settings: TwilioSmsSettings,
        request_cognition: Callable[..., Awaitable[str]],
        *, gateway: SmsSender | Any | None = None,
        media_downloader: MediaDownloader | None = None,
        inbox_size: int = SMS_INBOX_SIZE, dedupe_size: int = SMS_DEDUPE_SIZE,
        outbound_media: OutboundMediaStore | None = None,
        media_reap_interval_seconds: float = OUTBOUND_MEDIA_REAP_INTERVAL_SECONDS,
        image_limits: ImageLimits = ImageLimits(), media_store=None,
    ) -> None:
        self.image_limits, self.media_store = image_limits, media_store
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
        self._media_reaper: asyncio.Task[None] | None = None
        self._accepting = False
        self._outbound_media = outbound_media or OutboundMediaStore()
        self._media_reap_interval_seconds = media_reap_interval_seconds

    @property
    def image_delivery_available(self) -> bool:
        return bool(self._accepting and self._gateway is not None
                    and self.settings.public_media_base_url)

    async def deliver_image(self, image: OutboundImage) -> ImageDeliveryEvidence:
        """Stage and submit exactly one JPEG or PNG to the configured operator."""
        if not self.image_delivery_available:
            raise RuntimeError("outbound MMS is not configured or ready")
        if image.media_type not in ("image/jpeg", "image/png") or not _matches_magic(image.media_type, image.data):
            raise ValueError("outbound MMS requires a valid JPEG or PNG image")
        if not image.data or len(image.data) > MAX_OUTBOUND_IMAGE_BYTES:
            raise ValueError("outbound image exceeds the byte limit")
        caption = image.caption.strip()
        token = self._outbound_media.stage(image.data, image.media_type)
        extension = "jpg" if image.media_type == "image/jpeg" else "png"
        media_url = f"{self.settings.public_media_base_url}{OUTBOUND_MEDIA_PATH}{token}.{extension}"
        attempt = delivery_attempt(channel=self.channel, kind='image')
        try:
            response = await asyncio.to_thread(
                self._gateway.send_media, from_=self.settings.twilio_number,
                to=self.settings.operator_number, body=caption, media_url=media_url,
            )
        except asyncio.CancelledError:
            attempt.finish('submission_unconfirmed', failure='cancelled')
            # Submission may already have happened in the worker thread. Retain media
            # for the fetch window and do not invite an automatic duplicate.
            raise
        except Exception as error:
            if _provider_failure_is_rejection(error):
                attempt.finish('provider_rejected', failure='provider_rejected')
                self._outbound_media.remove(token)
                return ImageDeliveryEvidence("rejected", reason="provider_rejected")
            # Timeouts, connection failures, and unknown transport errors may happen
            # after submission. Retain the bearer and never automatically resend.
            attempt.finish('submission_unconfirmed', failure='transport_error')
            return ImageDeliveryEvidence("uncertain", reason="submission_uncertain")
        attempt.finish('provider_accepted', provider_reference=getattr(response, 'sid', None))
        return ImageDeliveryEvidence(
            "accepted",
            _bounded_provider_value(getattr(response, "sid", None), 64),
            _bounded_provider_value(getattr(response, "status", None), 32),
        )

    @property
    def channel(self) -> InteractionChannel:
        return InteractionChannel.REMOTE_TEXT

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
        attempt = delivery_attempt(channel=self.channel,
            kind='report' if message.report_source is not None else 'message',
            report_source=message.report_source)
        try:
            receipt = await asyncio.to_thread(
                self._gateway.send, from_=self.settings.twilio_number,
                to=self.settings.operator_number, body=body,
            )
        except asyncio.CancelledError:
            attempt.finish('submission_unconfirmed', failure='cancelled')
            raise
        except Exception as error:
            rejected = _provider_failure_is_rejection(error)
            attempt.finish('provider_rejected' if rejected else 'submission_unconfirmed',
                           failure='provider_rejected' if rejected else 'transport_error')
            LOGGER.error("[SMS] operator_delivery status=failed reason=provider_error")
            raise
        else:
            attempt.finish('provider_accepted', provider_reference=getattr(receipt, 'sid', None))
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
        app.router.add_route("GET", OUTBOUND_MEDIA_PATH + "{token}.jpg", self._serve_media)
        app.router.add_route("HEAD", OUTBOUND_MEDIA_PATH + "{token}.jpg", self._serve_media)
        app.router.add_route("GET", OUTBOUND_MEDIA_PATH + "{token}.png", self._serve_media)
        app.router.add_route("HEAD", OUTBOUND_MEDIA_PATH + "{token}.png", self._serve_media)
        # Bearer paths are credentials. Disable aiohttp's request-line access log;
        # _serve_media emits only sanitized method/status metadata.
        runner = web.AppRunner(app, access_log=None)
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
        self._media_reaper = asyncio.create_task(
            self._reap_outbound_media(), name="sms-outbound-media-reaper"
        )
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
        reaper, self._media_reaper = self._media_reaper, None
        if reaper is not None:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
        if runner is not None or worker is not None:
            LOGGER.info("[SMS] status=stopped")
        self._outbound_media.clear()

    async def _serve_media(self, request: Any) -> Any:
        from aiohttp import web
        token = request.match_info.get("token", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
            LOGGER.info("[SMS] outbound_media method=%s status=not_found", request.method)
            return web.Response(status=404)
        image = self._outbound_media.get_image(token)
        if image is None:
            LOGGER.info("[SMS] outbound_media method=%s status=not_found", request.method)
            return web.Response(status=404)
        data, media_type = image.data, image.media_type
        LOGGER.info("[SMS] outbound_media method=%s status=served bytes=%s",
                    request.method, len(data))
        extension = "png" if media_type == "image/png" else "jpg"
        return web.Response(
            body=None if request.method == "HEAD" else data,
            content_type=media_type,
            headers={"Content-Length": str(len(data)),
                     "Content-Disposition": f'inline; filename="camera.{extension}"',
                     "Cache-Control": "private, max-age=0, no-store"},
        )

    async def _reap_outbound_media(self) -> None:
        while True:
            await asyncio.sleep(self._media_reap_interval_seconds)
            self._outbound_media.expire()

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
        if media_count > self.image_limits.max_images:
            return web.Response(status=413, text="Image count exceeds configured maximum")
        if media_count:
            for key in (name for index in range(media_count)
                        for name in (f"MediaUrl{index}", f"MediaContentType{index}")):
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
        if media_count > self.image_limits.max_images:
            return _Acceptance(413, "image_count_exceeds_limit")
        if any(not form.get(name) for index in range(media_count)
               for name in (f"MediaUrl{index}", f"MediaContentType{index}")):
            return _Acceptance(400, "missing_fields")
        if any(_normalized_media_type(form[f"MediaContentType{index}"]) not in SUPPORTED_INTERACTION_IMAGE_TYPES
               for index in range(media_count)):
            return _Acceptance(415, "unsupported_media_type")
        sid = form["MessageSid"]
        persisted = False
        if self.media_store is not None:
            try:
                ingestion = self.media_store.ingestion_state(InteractionChannel.REMOTE_TEXT.value, sid)
                persisted = ingestion is not None and ingestion['status'] != 'partial'
            except Exception:
                LOGGER.warning("[SMS] durable_dedupe status=unavailable")
        if sid in self._accepted or persisted:
            LOGGER.info("[SMS] inbound message_sid=%s status=duplicate", sid)
            return _Acceptance(200, "duplicate")
        media_items = tuple(TwilioMediaReference(form[f"MediaUrl{index}"], form[f"MediaContentType{index}"])
                            for index in range(media_count))
        message = SmsInboundMessage(sid, form["From"], form["Body"],
            media_items[0] if media_items else None, media_items, datetime.now(UTC))
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

    def _forget(self, sid):
        """Release a failed partial intake so a webhook retry can recover it."""
        self._accepted.discard(sid)
        self._accepted_fifo = deque(item for item in self._accepted_fifo if item != sid)

    def _finish_media_ingestion(self, message, status):
        try:
            self.media_store.finish_ingestion(self.channel.value, message.message_sid, status)
        except Exception:
            LOGGER.warning("[SMS] ingestion_outcome status=unavailable")

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
                attachments = []
                media_items = message.media_items or ((message.media,) if message.media is not None else ())
                tracking = False
                received_at = message.received_at or datetime.now(UTC)
                if media_items and self.media_store is not None:
                    try:
                        ingestion = self.media_store.begin_ingestion(self.channel.value,
                            message.message_sid, len(media_items), received_at)
                        if ingestion['status'] != 'partial':
                            continue
                        received_at = datetime.fromisoformat(ingestion['received_at'])
                        tracking = True
                    except ValueError:
                        self._forget(message.message_sid)
                        await self._send_reply(message, MEDIA_FAILURE_REPLY)
                        continue
                    except Exception:
                        LOGGER.warning("[SMS] ingestion_state status=unavailable")
                try:
                    for index, reference in enumerate(media_items):
                        image = None
                        if self.media_store is not None:
                            try:
                                image = self.media_store.load_message_attachment(self.channel.value,
                                    message.message_sid, index, limits=self.image_limits)
                            except MediaUnavailableError:
                                raise MediaDownloadError("retained_media_unavailable") from None
                            except Exception:
                                LOGGER.warning("[SMS] retained_attachment status=unavailable")
                        if image is None:
                            image = await self._media_downloader.download(reference)
                            image = image.with_context(source="sms", source_message_id=message.message_sid,
                                attachment_index=index, received_at=received_at, label=f"attachment {index + 1}")
                        if image.media_type != _normalized_media_type(reference.declared_media_type):
                            raise MediaDownloadError("media_type_mismatch")
                        self.image_limits.validate((*attachments, image), decode=True)
                        attachments.append(image)
                        if self.media_store is not None:
                            try:
                                # Commit each accepted attachment before attempting the next download.
                                self.media_store.retain((image,), channel=self.channel.value,
                                    message_identity=message.message_sid, message_text=message.body,
                                    limits=self.image_limits)
                            except Exception:
                                LOGGER.warning("[SMS] retention status=failed_not_saved")
                        LOGGER.info("[SMS] media message_sid=%s attachment=%s type=%s bytes=%s status=loaded",
                                    message.message_sid, index, image.media_type, len(image.data))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._forget(message.message_sid)
                    LOGGER.info("[SMS] media message_sid=%s status=partial reason=invalid_or_oversized_media", message.message_sid)
                    await self._send_reply(message, MEDIA_FAILURE_REPLY)
                    continue
                attachments = tuple(attachments)
                if tracking:
                    try:
                        if not self.media_store.claim_ingestion(self.channel.value, message.message_sid):
                            continue
                    except Exception:
                        LOGGER.warning("[SMS] ingestion_claim status=unavailable")
                        tracking = False
                try:
                    arguments = {"interaction": interaction}
                    if attachments:
                        arguments["image_attachments"] = attachments
                    response = await self._request_cognition(message.body, **arguments)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if tracking:
                        self._finish_media_ingestion(message, 'failed')
                    LOGGER.error("[SMS] cognition status=failed")
                    continue
                if tracking:
                    self._finish_media_ingestion(message, 'completed')
                body = response.strip()
                if not body:
                    delivery_attempt(response, channel=self.channel).finish(
                        'not_submitted', failure='empty_response')
                    LOGGER.info("[SMS] reply status=failed reason=empty")
                    continue
                if len(body) > MAX_SMS_BODY_CHARS:
                    LOGGER.info("[SMS] reply status=failed reason=too_long")
                    body = TOO_LONG_REPLY
                await self._send_reply(message, body, response=response,
                    replaced=body != response.strip())
            finally:
                self._inbox.task_done()

    async def _send_reply(self, message: SmsInboundMessage, body: str, *,
                          response=None, replaced=False) -> None:
        attempt = delivery_attempt(response, channel=self.channel,
            output_variant='sms_length_explanation' if replaced else 'authored_output')
        try:
            receipt = await asyncio.to_thread(
                self._gateway.send, from_=self.settings.twilio_number,
                to=message.sender, body=body,
            )
        except asyncio.CancelledError:
            attempt.finish('submission_unconfirmed', failure='cancelled')
            raise
        except Exception as error:
            rejected = _provider_failure_is_rejection(error)
            attempt.finish('provider_rejected' if rejected else 'submission_unconfirmed',
                           failure='provider_rejected' if rejected else 'transport_error')
            LOGGER.error("[SMS] reply status=failed reason=provider_error")
        else:
            attempt.finish('provider_accepted', provider_reference=getattr(receipt, 'sid', None),
                           failure='response_replaced' if replaced else None)
            LOGGER.info("[SMS] reply message_sid=%s chars=%s text=%r status=sent",
                        message.message_sid, len(body), body)
