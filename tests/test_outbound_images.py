import asyncio
import importlib.util
import unittest
from types import SimpleNamespace

from embodied_runtime.outbound_images import OutboundImage
from embodied_runtime.sms import OutboundMediaStore, TwilioSmsService, TwilioSmsSettings


JPEG = b"\xff\xd8\xff" + b"camera-bytes"
HAS_AIOHTTP = importlib.util.find_spec("aiohttp") is not None


class Gateway:
    def __init__(self):
        self.calls = []

    def send_media(self, **arguments):
        self.calls.append(arguments)
        return SimpleNamespace(sid="MM123", status="queued")

    def validate(self, *_arguments):
        return True


class TwilioRestException(Exception):
    status = 400


TwilioRestException.__module__ = "twilio.base.exceptions"


class OutboundMediaStoreTests(unittest.TestCase):
    def test_exact_bytes_expire_and_capacity_is_rejected(self):
        now = [10.0]
        store = OutboundMediaStore(
            ttl_seconds=5, max_items=1, max_bytes=len(JPEG), clock=lambda: now[0]
        )
        token = store.stage(JPEG)
        self.assertEqual(store.get(token), JPEG)
        with self.assertRaisesRegex(RuntimeError, "capacity"):
            store.stage(JPEG)
        now[0] = 15.0
        self.assertIsNone(store.get(token))

    def test_clear_removes_all_content(self):
        store = OutboundMediaStore()
        token = store.stage(JPEG)
        store.clear()
        self.assertIsNone(store.get(token))


class TwilioOutboundImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_submission_uses_configured_numbers_and_staged_exact_bytes(self):
        gateway = Gateway()
        store = OutboundMediaStore()
        settings = TwilioSmsSettings(
            "AC123", "secret", "+15550000001", "+15550000002",
            "https://public.example/sms", "127.0.0.1", 8080, "/sms",
            "https://public.example",
        )
        service = TwilioSmsService(settings, lambda _: None, gateway=gateway,
                                   outbound_media=store)
        service._accepting = True
        evidence = await service.deliver_image(
            OutboundImage(JPEG, "image/jpeg", "Fresh camera photo")
        )
        self.assertEqual(evidence.status, "accepted")
        self.assertEqual(evidence.provider_reference, "MM123")
        self.assertEqual(evidence.provider_status, "queued")
        call = gateway.calls[0]
        self.assertEqual(call["to"], "+15550000002")
        self.assertEqual(call["from_"], "+15550000001")
        token = call["media_url"].rsplit("/", 1)[1].removesuffix(".jpg")
        self.assertEqual(store.get(token), JPEG)

    async def test_rejects_non_jpeg_without_staging_or_submission(self):
        gateway = Gateway()
        settings = TwilioSmsSettings(
            "AC123", "secret", "+15550000001", "+15550000002",
            "https://public.example/sms", "127.0.0.1", 8080, "/sms",
            "https://public.example",
        )
        service = TwilioSmsService(settings, lambda _: None, gateway=gateway)
        service._accepting = True
        with self.assertRaisesRegex(ValueError, "valid JPEG"):
            await service.deliver_image(OutboundImage(b"bad", "image/jpeg", ""))
        self.assertEqual(gateway.calls, [])

    async def test_rejection_removes_media_but_ambiguous_failure_retains_it(self):
        class FailingGateway(Gateway):
            error = TwilioRestException("provider detail must not escape")

            def send_media(self, **arguments):
                self.calls.append(arguments)
                raise self.error

        settings = TwilioSmsSettings(
            "AC123", "secret", "+15550000001", "+15550000002",
            "https://public.example/sms", "127.0.0.1", 8080, "/sms",
            "https://public.example",
        )
        store = OutboundMediaStore()
        gateway = FailingGateway()
        service = TwilioSmsService(settings, lambda _: None, gateway=gateway,
                                   outbound_media=store)
        service._accepting = True
        rejected = await service.deliver_image(OutboundImage(JPEG, "image/jpeg", ""))
        self.assertEqual((rejected.status, rejected.reason),
                         ("rejected", "provider_rejected"))
        self.assertEqual(store.retained_count, 0)

        gateway.error = ConnectionError("ambiguous secret endpoint")
        uncertain = await service.deliver_image(OutboundImage(JPEG, "image/jpeg", ""))
        self.assertEqual((uncertain.status, uncertain.reason),
                         ("uncertain", "submission_uncertain"))
        self.assertEqual(store.retained_count, 1)

        gateway.error = TimeoutError("read timed out after POST")
        uncertain = await service.deliver_image(OutboundImage(JPEG, "image/jpeg", ""))
        self.assertEqual(uncertain.status, "uncertain")
        self.assertEqual(store.retained_count, 2)
        self.assertEqual(len(gateway.calls), 3)  # exactly one attempt per invocation

    async def test_cancelled_submission_retains_staging(self):
        class SlowGateway(Gateway):
            def send_media(self, **arguments):
                self.calls.append(arguments)
                import time
                time.sleep(0.1)
                return SimpleNamespace(sid="late", status="queued")

        store = OutboundMediaStore()
        service = TwilioSmsService(TwilioSmsSettings(
            "AC123", "secret", "+15550000001", "+15550000002",
            "https://public.example/sms", "127.0.0.1", 8080, "/sms",
            "https://public.example"), lambda _: None, gateway=SlowGateway(),
            outbound_media=store)
        service._accepting = True
        task = asyncio.create_task(service.deliver_image(
            OutboundImage(JPEG, "image/jpeg", "")))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(store.retained_count, 1)

    @unittest.skipUnless(HAS_AIOHTTP, "aiohttp optional dependency is unavailable")
    async def test_actual_get_and_head_hide_bearer_and_preserve_metadata(self):
        from aiohttp import ClientSession

        gateway = Gateway()
        store = OutboundMediaStore()
        service = TwilioSmsService(TwilioSmsSettings(
            "AC123", "secret", "+15550000001", "+15550000002",
            "https://public.example/sms", "127.0.0.1", 0, "/sms",
            "https://public.example"), lambda _: None, gateway=gateway,
            outbound_media=store)
        token = store.stage(JPEG)
        with self.assertLogs(level="INFO") as captured:
            await service.start()
            try:
                site = next(iter(service._runner.sites))
                port = site._server.sockets[0].getsockname()[1]
                url = f"http://127.0.0.1:{port}/outbound-media/{token}.jpg"
                async with ClientSession() as client:
                    async with client.get(url) as response:
                        self.assertEqual(await response.read(), JPEG)
                        self.assertEqual(response.headers["Content-Type"], "image/jpeg")
                        self.assertEqual(response.headers["Content-Length"], str(len(JPEG)))
                    async with client.head(url) as response:
                        self.assertEqual(await response.read(), b"")
                        self.assertEqual(response.headers["Content-Length"], str(len(JPEG)))
            finally:
                await service.stop()
        logs = "\n".join(captured.output)
        self.assertNotIn(token, logs)
        self.assertNotIn(url, logs)
        self.assertIn("outbound_media method=GET status=served", logs)
        self.assertIn("outbound_media method=HEAD status=served", logs)

    async def test_reaper_expires_without_a_followup_request_and_shutdown_clears(self):
        now = [0.0]
        store = OutboundMediaStore(ttl_seconds=1, clock=lambda: now[0])
        service = TwilioSmsService(TwilioSmsSettings(
            "AC123", "secret", "+15550000001", "+15550000002",
            "https://public.example/sms", "127.0.0.1", 0, "/sms",
            "https://public.example"), lambda _: None, gateway=Gateway(),
            outbound_media=store, media_reap_interval_seconds=0.01)
        reaper = asyncio.create_task(service._reap_outbound_media())
        first = store.stage(JPEG)
        now[0] = 2
        await asyncio.sleep(0.03)
        self.assertIsNone(store.get(first))
        second = store.stage(JPEG)
        reaper.cancel()
        await asyncio.gather(reaper, return_exceptions=True)
        store.clear()
        self.assertIsNone(store.get(second))


if __name__ == "__main__":
    unittest.main()
