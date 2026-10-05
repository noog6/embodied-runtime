import unittest
from types import SimpleNamespace

from embodied_runtime.outbound_images import OutboundImage
from embodied_runtime.sms import OutboundMediaStore, TwilioSmsService, TwilioSmsSettings


JPEG = b"\xff\xd8\xff" + b"camera-bytes"


class Gateway:
    def __init__(self):
        self.calls = []

    def send_media(self, **arguments):
        self.calls.append(arguments)
        return SimpleNamespace(sid="MM123", status="queued")


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


if __name__ == "__main__":
    unittest.main()
