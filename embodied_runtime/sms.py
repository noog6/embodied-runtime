"""Narrow Twilio transport for inbound, bounded operator SMS dialogue."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import logging
import os
import re
from typing import Any, Protocol
from urllib.parse import urlsplit

from embodied_runtime.interaction import (
    InteractionCadence, InteractionChannel, InteractionContext,
    InteractionInitiator, InteractionMode,
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
_E164 = re.compile(r"\+[1-9][0-9]{1,14}\Z")
_REQUIRED_ENV = (
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER",
    "MIRA_SMS_OPERATOR_NUMBER", "TWILIO_WEBHOOK_URL",
)


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
        return cls(
            values["TWILIO_ACCOUNT_SID"], values["TWILIO_AUTH_TOKEN"],
            twilio_number, operator_number, webhook_url, bind_host, bind_port,
            webhook_path,
        )


@dataclass(frozen=True, slots=True)
class SmsInboundMessage:
    message_sid: str
    sender: str
    body: str


class SmsSender(Protocol):
    def send(self, *, from_: str, to: str, body: str) -> None: ...


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

    def send(self, *, from_: str, to: str, body: str) -> None:
        self._client.messages.create(from_=from_, to=to, body=body)


class TwilioSmsService:
    """Own one aiohttp endpoint, bounded FIFO, dedupe cache, and worker."""

    def __init__(
        self, settings: TwilioSmsSettings,
        request_cognition: Callable[..., Awaitable[str]],
        *, gateway: SmsSender | Any | None = None,
        inbox_size: int = SMS_INBOX_SIZE, dedupe_size: int = SMS_DEDUPE_SIZE,
    ) -> None:
        self.settings = settings
        self._request_cognition = request_cognition
        self._gateway = gateway
        self._inbox: asyncio.Queue[SmsInboundMessage] = asyncio.Queue(inbox_size)
        self._dedupe_size = dedupe_size
        self._accepted: set[str] = set()
        self._accepted_fifo: deque[str] = deque()
        self._runner: Any = None
        self._worker: asyncio.Task[None] | None = None
        self._accepting = False

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
        # Only after validating the complete signed form do we select P1 fields.
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
        if media_count > 0:
            LOGGER.info("[SMS] inbound status=ignored reason=media_unsupported")
            return _Acceptance(200, "media_unsupported")
        if form["From"] != self.settings.operator_number:
            LOGGER.info("[SMS] inbound status=ignored reason=external_participant")
            return _Acceptance(200, "external_participant")
        sid = form["MessageSid"]
        if sid in self._accepted:
            LOGGER.info("[SMS] inbound message_sid=%s status=duplicate", sid)
            return _Acceptance(200, "duplicate")
        message = SmsInboundMessage(sid, form["From"], form["Body"])
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
        LOGGER.info("[SMS] inbound message_sid=%s status=accepted sender=operator", sid)
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
                try:
                    response = await self._request_cognition(
                        message.body, interaction=interaction
                    )
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
                try:
                    await asyncio.to_thread(
                        self._gateway.send, from_=self.settings.twilio_number,
                        to=message.sender, body=body,
                    )
                except Exception:
                    LOGGER.error("[SMS] reply status=failed reason=provider_error")
                else:
                    LOGGER.info("[SMS] reply message_sid=%s chars=%s status=sent",
                                message.message_sid, len(body))
            finally:
                self._inbox.task_done()
