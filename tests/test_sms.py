import asyncio
import threading
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
    EMPTY_TWIML, MAX_SMS_BODY_CHARS, TOO_LONG_REPLY, SmsConfigurationError,
    TwilioSmsService, TwilioSmsSettings,
)


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

    async def test_invalid_and_missing_signatures_fail_closed(self):
        self.assertEqual((await self.post(form(), valid=False)).status, 403)
        self.assertEqual((await self.post(form(), valid=None)).status, 403)
        self.assertEqual(self.calls, [])

    async def test_unknown_sender_and_media_are_acknowledged_without_cognition(self):
        self.assertEqual((await self.post(form(sender="+15550000003"))).status, 200)
        self.assertEqual((await self.post(form(sid="SM2", media="1"))).status, 200)
        await asyncio.sleep(0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.gateway.sent, [])

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
