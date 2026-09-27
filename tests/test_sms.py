import asyncio
import io
import logging
import sys
import threading
import types
import unittest
from unittest import mock

try:
    from aiohttp import ClientSession
    from multidict import MultiDict
    from twilio.request_validator import RequestValidator
except ImportError:
    ClientSession = None
    MultiDict = None
    RequestValidator = None

from embodied_runtime.interaction import (
    InteractionCadence, InteractionChannel, InteractionEnvironment,
    InteractionInitiator, InteractionMode,
)
from embodied_runtime.sms import (
    EMPTY_TWIML, MAX_SMS_BODY_CHARS, MEDIA_FAILURE_REPLY, TOO_LONG_REPLY,
    MediaDownloadError, SmsConfigurationError, TWILIO_HTTP_LOGGER, TwilioMediaDownloader,
    TwilioMediaReference, TwilioSmsGateway, TwilioSmsService, TwilioSmsSettings,
)
from embodied_runtime.attachments import ImageAttachment, MAX_INTERACTION_IMAGE_BYTES


AUTH_TOKEN = "test-auth-token-not-a-secret"
EXTERNAL_URL = "https://example.invalid/sms"


class FakeGateway:
    def __init__(self):
        self.validator = RequestValidator(AUTH_TOKEN)
        self.sent = []
        self.send_threads = []

    def validate(self, url, form, signature):
        return self.validator.validate(url, form, signature)

    def send(self, **message):
        self.send_threads.append(threading.get_ident())
        self.sent.append(message)


def settings(**changes):
    values = dict(
        account_sid="AC00000000000000000000000000000000",
        auth_token=AUTH_TOKEN,
        twilio_number="+15550000001",
        operator_number="+15550000002",
        webhook_url=EXTERNAL_URL,
        bind_host="127.0.0.1", bind_port=0, webhook_path="/sms",
    )
    values.update(changes)
    return TwilioSmsSettings(**values)


def form(sid="SM1", sender="+15550000002", body="hello", media="0"):
    return {
        "MessageSid": sid,
        "AccountSid": "AC00000000000000000000000000000000",
        "From": sender,
        "To": "+15550000001",
        "Body": body,
        "NumMedia": media,
    }


def media_form(**changes):
    payload = form(media="1")
    payload.update({
        "MediaUrl0": (
            "https://api.twilio.com/2010-04-01/Accounts/"
            "AC00000000000000000000000000000000/Messages/SM1/Media/ME1"
        ),
        "MediaContentType0": "image/jpeg",
    })
    payload.update(changes)
    return payload


class FakeMediaDownloader:
    def __init__(self, image=None):
        self.image = image or ImageAttachment("image/jpeg", b"\xff\xd8\xffdata")
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def download(self, reference):
        self.calls.append(reference)
        self.entered.set()
        await self.release.wait()
        return self.image


class FailIfCalledDownloader:
    def __init__(self):
        self.calls = 0

    async def download(self, reference):
        self.calls += 1
        raise AssertionError("external-participant media must not be downloaded")


def fake_aiohttp(session):
    """Return a complete offline seam for TwilioMediaDownloader's local import."""
    module = types.ModuleType("aiohttp")

    class BasicAuth:
        def __init__(self, login, password):
            self.login = login
            self.password = password

    class ClientTimeout:
        def __init__(self, **values):
            self.values = values

    module.BasicAuth = BasicAuth
    module.ClientTimeout = ClientTimeout
    module.ClientSession = mock.Mock(return_value=session)
    return module


class SmsSettingsTests(unittest.TestCase):
    def test_environment_is_strict_and_private_values_are_not_diagnostics(self):
        environment = {
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "private-token",
            "TWILIO_PHONE_NUMBER": "+15550000001",
            "MIRA_SMS_OPERATOR_NUMBER": "+15550000002",
            "TWILIO_WEBHOOK_URL": EXTERNAL_URL,
        }
        configured = TwilioSmsSettings.from_environment(
            bind_host="127.0.0.1", bind_port=8080, webhook_path="/sms",
            environ=environment,
        )
        service = TwilioSmsService(configured, mock.AsyncMock(), gateway=object())
        rendered = repr(service.diagnostics)
        for private in environment.values():
            self.assertNotIn(private, rendered)

        for invalid in ("15550000001", "+01", "+1 555 000 0001"):
            with self.subTest(invalid=invalid), self.assertRaises(SmsConfigurationError):
                TwilioSmsSettings.from_environment(
                    bind_host="x", bind_port=1, webhook_path="/sms",
                    environ={**environment, "TWILIO_PHONE_NUMBER": invalid},
                )
        with self.assertRaises(SmsConfigurationError):
            TwilioSmsSettings.from_environment(
                bind_host="x", bind_port=1, webhook_path="/sms",
                environ={**environment, "TWILIO_WEBHOOK_URL": "http://example.invalid/sms"},
            )


class TwilioLoggingTests(unittest.TestCase):
    def test_gateway_suppresses_only_twilio_http_diagnostics(self):
        twilio_package = types.ModuleType("twilio")
        twilio_package.__path__ = []
        validator_module = types.ModuleType("twilio.request_validator")
        rest_module = types.ModuleType("twilio.rest")

        class FakeValidator:
            def __init__(self, token):
                self.token = token

        class FakeClient:
            def __init__(self, account_sid, auth_token):
                logging.getLogger(TWILIO_HTTP_LOGGER).info(
                    "POST https://api.twilio.invalid/Accounts/%s token=%s",
                    account_sid, auth_token,
                )

        validator_module.RequestValidator = FakeValidator
        rest_module.Client = FakeClient
        modules = {
            "twilio": twilio_package,
            "twilio.request_validator": validator_module,
            "twilio.rest": rest_module,
        }

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        twilio_logger = logging.getLogger(TWILIO_HTTP_LOGGER)
        sms_logger = logging.getLogger("embodied_runtime.sms")
        unrelated_logger = logging.getLogger("embodied_runtime.test_unrelated")
        original_root_level = root.level
        original_twilio_level = twilio_logger.level
        original_sms_level = sms_logger.level
        original_unrelated_level = unrelated_logger.level
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        twilio_logger.setLevel(logging.NOTSET)
        sms_logger.setLevel(logging.NOTSET)
        unrelated_logger.setLevel(logging.NOTSET)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, original_root_level)
        self.addCleanup(twilio_logger.setLevel, original_twilio_level)
        self.addCleanup(sms_logger.setLevel, original_sms_level)
        self.addCleanup(unrelated_logger.setLevel, original_unrelated_level)

        with mock.patch.dict(sys.modules, modules):
            TwilioSmsGateway(settings())

        self.assertEqual(twilio_logger.level, logging.WARNING)
        logging.getLogger(TWILIO_HTTP_LOGGER).info("Response Headers: private")
        sms_logger.info("[SMS] reply message_sid=SM1 chars=7 status=sent")
        sms_logger.error("[SMS] reply status=failed reason=provider_error")
        unrelated_logger.info("unrelated application record")

        output = stream.getvalue()
        self.assertNotIn(settings().account_sid, output)
        self.assertNotIn(settings().auth_token, output)
        self.assertNotIn(settings().twilio_number, output)
        self.assertNotIn(settings().operator_number, output)
        self.assertNotIn(settings().webhook_url, output)
        self.assertNotIn("POST https://api.twilio.invalid", output)
        self.assertNotIn("Response Headers", output)
        self.assertIn("[SMS] reply message_sid=SM1 chars=7 status=sent", output)
        self.assertIn("[SMS] reply status=failed reason=provider_error", output)
        self.assertIn("unrelated application record", output)


class SmsStateMachineTests(unittest.IsolatedAsyncioTestCase):
    """Provider-independent queue, dedupe, and shutdown coverage."""

    def service(self, cognition=None, *, inbox_size=16):
        async def default_cognition(message, *, interaction):
            return "answer"
        return TwilioSmsService(
            settings(), cognition or default_cognition, gateway=object(),
            inbox_size=inbox_size,
        )

    async def test_queue_full_sid_is_retryable_after_capacity_returns(self):
        service = self.service(inbox_size=2)
        service._accepting = True
        self.assertEqual(service._accept_validated_form(form("SM1")).status, 200)
        self.assertEqual(service._accept_validated_form(form("SM2")).status, 200)
        self.assertEqual(service._inbox.qsize(), 2)

        rejected = service._accept_validated_form(form("SM3"))
        self.assertEqual((rejected.status, rejected.reason), (503, "queue_full"))
        self.assertNotIn("SM3", service._accepted)
        self.assertLessEqual(service._inbox.qsize(), 2)

        service._inbox.get_nowait()
        service._inbox.task_done()
        accepted = service._accept_validated_form(form("SM3"))
        self.assertEqual((accepted.status, accepted.reason), (200, "accepted"))
        self.assertIn("SM3", service._accepted)
        self.assertEqual(service._accept_validated_form(form("SM3")).reason, "duplicate")
        self.assertLessEqual(service._inbox.qsize(), 2)

    async def test_media_count_rejects_negative_and_malformed_values(self):
        service = self.service()
        service._accepting = True
        for value in ("-1", "bad"):
            with self.subTest(value=value):
                result = service._accept_validated_form(form(media=value))
                self.assertEqual((result.status, result.reason),
                                 (400, "invalid_media_count"))
        self.assertEqual(service._inbox.qsize(), 0)
        self.assertEqual(service._accepted, set())

    async def test_one_media_reference_is_queued_without_downloading(self):
        downloader = FakeMediaDownloader()
        service = TwilioSmsService(
            settings(), mock.AsyncMock(), gateway=object(), media_downloader=downloader,
        )
        service._accepting = True
        accepted = service._accept_validated_form(media_form())
        self.assertEqual((accepted.status, accepted.reason), (200, "accepted"))
        queued = service._inbox.get_nowait()
        self.assertEqual(queued.media, TwilioMediaReference(
            media_form()["MediaUrl0"], "image/jpeg"
        ))
        self.assertEqual(downloader.calls, [])

    async def test_worker_download_is_sequential_and_image_reaches_cognition(self):
        downloader = FakeMediaDownloader()
        calls = []

        async def cognition(message, *, interaction, image_attachments):
            calls.append((message, interaction, image_attachments))
            return "answer"

        gateway = mock.Mock()
        service = TwilioSmsService(
            settings(), cognition, gateway=gateway, media_downloader=downloader,
        )
        service._accepting = True
        service._worker = asyncio.create_task(service._run_worker())
        self.assertEqual(service._accept_validated_form(
            media_form(Body="")
        ).status, 200)
        await downloader.entered.wait()
        self.assertEqual(calls, [])
        downloader.release.set()
        await service._inbox.join()
        self.assertEqual(calls[0][0], "")
        self.assertEqual(calls[0][2], (downloader.image,))
        self.assertEqual(calls[0][1].channel, InteractionChannel.REMOTE_TEXT)
        await service.stop()


class TwilioMediaSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.downloader = TwilioMediaDownloader(settings())

    @staticmethod
    def service(cognition):
        return TwilioSmsService(settings(), cognition, gateway=object())

    def test_only_exact_twilio_account_message_media_urls_are_allowed(self):
        valid = media_form()["MediaUrl0"]
        self.downloader._validate_url(valid)
        invalid = (
            valid.replace("https://", "http://"),
            valid.replace("api.twilio.com", "example.com"),
            valid.replace("https://", "https://user:password@"),
            valid.replace(settings().account_sid, "ACwrong"),
            valid + "?download=1",
            "https://api.twilio.com/2010-04-01/Accounts/"
            f"{settings().account_sid}/Messages/SM1",
        )
        for url in invalid:
            with self.subTest(url=url), self.assertRaisesRegex(
                MediaDownloadError, "invalid_media_url"
            ):
                self.downloader._validate_url(url)

    def test_magic_bytes_cover_only_p2_image_types(self):
        from embodied_runtime.sms import _matches_magic
        accepted = (
            ("image/jpeg", b"\xff\xd8\xffrest"),
            ("image/png", b"\x89PNG\r\n\x1a\nrest"),
            ("image/webp", b"RIFF1234WEBPrest"),
        )
        for media_type, data in accepted:
            with self.subTest(media_type=media_type):
                self.assertTrue(_matches_magic(media_type, data))
        for media_type in ("image/gif", "image/heic", "video/mp4", "audio/aac",
                           "application/pdf"):
            self.assertFalse(_matches_magic(media_type, b"anything"))

    def test_runtime_image_bound_is_four_mib(self):
        self.assertEqual(MAX_INTERACTION_IMAGE_BYTES, 4 * 1024 * 1024)

    async def test_download_streams_with_basic_auth_and_no_redirect_or_retry(self):
        class Content:
            async def iter_chunked(self, size):
                yield b"\xff\xd8"
                yield b"\xffphoto"

        response = types.SimpleNamespace(
            status=200, content_length=None,
            headers={"Content-Type": "image/jpeg; charset=binary"}, content=Content(),
        )

        class Context:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *args):
                return False

        class Session:
            def __init__(self):
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Context()

        session = Session()
        reference = TwilioMediaReference(media_form()["MediaUrl0"], "image/jpeg")
        with mock.patch.dict(sys.modules, {"aiohttp": fake_aiohttp(session)}):
            image = await self.downloader.download(reference)
        self.assertEqual(image, ImageAttachment("image/jpeg", b"\xff\xd8\xffphoto"))
        self.assertEqual(len(session.calls), 1)
        url, arguments = session.calls[0]
        self.assertEqual(url, reference.url)
        self.assertNotIn(settings().auth_token, url)
        self.assertFalse(arguments["allow_redirects"])
        self.assertEqual(arguments["auth"].login, settings().account_sid)
        self.assertEqual(arguments["auth"].password, settings().auth_token)

    async def test_declared_and_streamed_oversize_are_rejected(self):
        class Content:
            def __init__(self, chunk):
                self.chunk = chunk

            async def iter_chunked(self, size):
                yield self.chunk

        class Context:
            def __init__(self, length, chunk):
                self.response = types.SimpleNamespace(
                    status=200, content_length=length,
                    headers={"Content-Type": "image/png"}, content=Content(chunk),
                )

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, *args):
                return False

        class Session:
            def __init__(self, context):
                self.context = context

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, *args, **kwargs):
                return self.context

        reference = TwilioMediaReference(media_form()["MediaUrl0"], "image/png")
        cases = (
            Context(MAX_INTERACTION_IMAGE_BYTES + 1, b""),
            Context(None, b"x" * (MAX_INTERACTION_IMAGE_BYTES + 1)),
        )
        for context in cases:
            session = Session(context)
            with self.subTest(length=context.response.content_length), mock.patch.dict(
                sys.modules, {"aiohttp": fake_aiohttp(session)}
            ), self.assertRaisesRegex(MediaDownloadError, "media_too_large"):
                await self.downloader.download(reference)

    async def test_mime_magic_timeout_and_status_fail_boundedly(self):
        class Content:
            def __init__(self, data):
                self.data = data

            async def iter_chunked(self, size):
                yield self.data

        class Context:
            def __init__(self, *, status=200, media_type="image/jpeg", data=b"\xff\xd8\xff"):
                self.response = types.SimpleNamespace(
                    status=status, content_length=len(data),
                    headers={"Content-Type": media_type}, content=Content(data),
                )

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, *args):
                return False

        class TimeoutContext:
            async def __aenter__(self):
                raise asyncio.TimeoutError

            async def __aexit__(self, *args):
                return False

        class Session:
            def __init__(self, context):
                self.context = context
                self.calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, *args, **kwargs):
                self.calls += 1
                return self.context

        reference = TwilioMediaReference(media_form()["MediaUrl0"], "image/jpeg")
        cases = (
            (Context(media_type="image/png"), "media_type_mismatch"),
            (Context(data=b"not-a-jpeg"), "media_type_mismatch"),
            (TimeoutContext(), "media_download_timeout"),
            (Context(status=500), "media_download_failed"),
        )
        for context, reason in cases:
            session = Session(context)
            with self.subTest(reason=reason), mock.patch.dict(
                sys.modules, {"aiohttp": fake_aiohttp(session)}
            ), self.assertRaisesRegex(MediaDownloadError, reason):
                await self.downloader.download(reference)
            self.assertEqual(session.calls, 1)

    async def test_media_failure_replies_once_and_worker_remains_usable(self):
        class RejectingDownloader:
            async def download(self, reference):
                raise MediaDownloadError("media_type_mismatch")

        cognition = mock.AsyncMock(return_value="plain answer")
        gateway = mock.Mock()
        service = TwilioSmsService(
            settings(), cognition, gateway=gateway,
            media_downloader=RejectingDownloader(),
        )
        service._accepting = True
        service._worker = asyncio.create_task(service._run_worker())
        self.assertEqual(service._accept_validated_form(media_form()).status, 200)
        self.assertEqual(service._accept_validated_form(form("SM2", body="next")).status, 200)
        await service._inbox.join()
        cognition.assert_awaited_once()
        self.assertEqual(cognition.await_args.args, ("next",))
        self.assertEqual(gateway.send.call_count, 2)
        self.assertEqual(
            [call.kwargs["body"] for call in gateway.send.call_args_list],
            [MEDIA_FAILURE_REPLY, "plain answer"],
        )
        await service.stop()

    async def test_shutdown_cancels_active_worker_and_never_starts_queued_work(self):
        entered = asyncio.Event()
        blocked = asyncio.Event()
        calls = []

        async def cognition(message, *, interaction):
            calls.append(message)
            entered.set()
            await blocked.wait()
            return "answer"

        service = self.service(cognition)
        service._accepting = True
        service._worker = asyncio.create_task(service._run_worker())
        self.assertEqual(service._accept_validated_form(form("SM1", body="one")).status, 200)
        self.assertEqual(service._accept_validated_form(form("SM2", body="two")).status, 200)
        await entered.wait()

        stop_task = asyncio.create_task(service.stop())
        await stop_task
        self.assertEqual(calls, ["one"])
        self.assertIsNone(service._worker)
        self.assertFalse(service._accepting)
        self.assertEqual(service._inbox.qsize(), 1)

        racing = service._accept_validated_form(form("SM3", body="three"))
        self.assertEqual((racing.status, racing.reason), (503, "stopping"))
        self.assertNotIn("SM3", service._accepted)
        self.assertEqual(calls, ["one"])


@unittest.skipIf(RequestValidator is None, "Twilio SMS optional dependencies not installed")
class SmsServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = FakeGateway()
        self.calls = []
        self.release = asyncio.Event()

        async def cognition(message, *, interaction):
            self.calls.append((message, interaction))
            await self.release.wait()
            return "bounded answer"

        self.service = TwilioSmsService(settings(), cognition, gateway=self.gateway)
        await self.service.start()
        port = self.service._runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{port}/sms"
        self.session = ClientSession()

    async def asyncTearDown(self):
        self.release.set()
        await self.service.stop()
        await self.session.close()

    async def post(self, payload, *, valid=True, headers=None):
        signature = RequestValidator(AUTH_TOKEN).compute_signature(EXTERNAL_URL, payload)
        request_headers = dict(headers or {})
        if valid is not None:
            request_headers["X-Twilio-Signature"] = signature if valid else "invalid"
        return await self.session.post(self.url, data=payload, headers=request_headers)

    async def test_signature_preserves_unknown_and_repeated_form_values(self):
        payload = MultiDict(list(form().items()) + [
            ("FutureField", "future"), ("RepeatedField", "one"),
            ("RepeatedField", "two"),
        ])
        signature = RequestValidator(AUTH_TOKEN).compute_signature(EXTERNAL_URL, payload)
        response = await self.session.post(
            self.url, data=payload, headers={"X-Twilio-Signature": signature},
        )
        self.assertEqual(response.status, 200)
        await asyncio.sleep(0)
        self.assertEqual(len(self.calls), 1)
        self.release.set()
        await self.service._inbox.join()

    async def test_signed_repeated_required_field_is_ambiguous(self):
        payload = MultiDict(list(form().items()))
        payload.add("From", "+15550000003")
        signature = RequestValidator(AUTH_TOKEN).compute_signature(EXTERNAL_URL, payload)
        response = await self.session.post(
            self.url, data=payload, headers={"X-Twilio-Signature": signature},
        )
        self.assertEqual(response.status, 400)
        await asyncio.sleep(0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.service._inbox.qsize(), 0)
        self.assertEqual(self.service._accepted, set())
        self.assertEqual(self.gateway.sent, [])

    async def test_malformed_form_is_400_without_side_effects(self):
        class BrokenRequest:
            headers = {}

            async def post(self):
                raise ValueError("malformed")

        response = await self.service._handle_webhook(BrokenRequest())
        self.assertEqual(response.status, 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.service._accepted, set())
        self.assertEqual(self.gateway.sent, [])

    async def test_valid_request_acknowledges_before_cognition_and_maps_context(self):
        loop_thread = threading.get_ident()
        response = await self.post({**form(), "FutureField": "signed-too"})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), EMPTY_TWIML)
        await asyncio.sleep(0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.gateway.sent, [])
        interaction = self.calls[0][1]
        self.assertEqual(interaction.channel, InteractionChannel.REMOTE_TEXT)
        self.assertEqual(interaction.mode, InteractionMode.DIALOGUE)
        self.assertEqual(interaction.cadence, InteractionCadence.BOUNDED_TURN)
        self.assertEqual(interaction.initiator, InteractionInitiator.OPERATOR)
        self.assertTrue(interaction.response_expected)
        self.release.set()
        await self.service._inbox.join()
        self.assertEqual(self.gateway.sent, [{
            "from_": "+15550000001", "to": "+15550000002", "body": "bounded answer"
        }])
        self.assertNotEqual(self.gateway.send_threads, [loop_thread])

    async def test_mms_acknowledges_while_media_download_is_blocked(self):
        downloader = FakeMediaDownloader()
        calls = []

        async def cognition(message, *, interaction, image_attachments):
            calls.append((message, image_attachments))
            return "image answer"

        self.service._media_downloader = downloader
        self.service._request_cognition = cognition
        response = await self.post(media_form())
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), EMPTY_TWIML)
        await downloader.entered.wait()
        self.assertEqual(calls, [])
        self.assertEqual(self.gateway.sent, [])
        downloader.release.set()
        await self.service._inbox.join()
        self.assertEqual(calls, [("hello", (downloader.image,))])
        self.assertEqual(self.gateway.sent[-1]["body"], "image answer")

    async def test_repeated_media_projection_is_rejected_without_dedupe(self):
        for field in ("MediaUrl0", "MediaContentType0"):
            payload = MultiDict(list(media_form().items()))
            payload.add(field, "ambiguous")
            signature = RequestValidator(AUTH_TOKEN).compute_signature(EXTERNAL_URL, payload)
            response = await self.session.post(
                self.url, data=payload, headers={"X-Twilio-Signature": signature},
            )
            self.assertEqual(response.status, 400)
        self.assertEqual(self.service._accepted, set())

    async def test_invalid_and_missing_signatures_fail_closed(self):
        self.assertEqual((await self.post(form(), valid=False)).status, 403)
        self.assertEqual((await self.post(form(), valid=None)).status, 403)
        self.assertEqual(self.calls, [])

    async def test_unknown_sender_and_media_are_acknowledged_without_cognition(self):
        downloader = FailIfCalledDownloader()
        self.service._media_downloader = downloader
        self.assertEqual((await self.post(form(sender="+15550000003"))).status, 200)
        external_mms = media_form(MessageSid="SM2", From="+15550000003")
        self.assertEqual((await self.post(external_mms)).status, 200)
        # Sender classification precedes operator-only media projection, so even
        # absent media fields are irrelevant for an authenticated external sender.
        unprojected = form("SM3", sender="+15550000003", media="1")
        self.assertEqual((await self.post(unprojected)).status, 200)
        await asyncio.sleep(0)
        self.assertEqual(downloader.calls, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.gateway.sent, [])
        self.assertEqual(self.service._accepted, set())

    async def test_duplicate_is_processed_once_but_new_sid_is_distinct(self):
        self.assertEqual((await self.post(form())).status, 200)
        self.assertEqual((await self.post(form())).status, 200)
        self.assertEqual((await self.post(form(sid="SM2"))).status, 200)
        await asyncio.sleep(0)
        self.assertEqual(len(self.calls), 1)
        self.release.set()
        await self.service._inbox.join()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.gateway.sent), 2)

    async def test_oversized_reply_uses_deterministic_fallback(self):
        async def long_cognition(message, *, interaction):
            return "x" * (MAX_SMS_BODY_CHARS + 1)

        await self.service.stop()
        self.service = TwilioSmsService(settings(), long_cognition, gateway=self.gateway)
        await self.service.start()
        port = self.service._runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{port}/sms"
        self.assertEqual((await self.post(form())).status, 200)
        await self.service._inbox.join()
        self.assertEqual(self.gateway.sent[-1]["body"], TOO_LONG_REPLY)


class SmsAuthorityTests(unittest.TestCase):
    def test_sms_does_not_define_environment_or_delivery_authority(self):
        self.assertEqual(InteractionEnvironment.WORKSTATION.value, "workstation")
        self.assertNotIn("sms", [mode.value for mode in InteractionMode])
