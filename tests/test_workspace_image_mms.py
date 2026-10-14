"""Saved PNGs use the existing bounded MMS submission and bearer fetch path."""

import importlib.util
import unittest

from embodied_runtime.outbound_images import OutboundImage
from embodied_runtime.sms import OutboundMediaStore, TwilioSmsService, TwilioSmsSettings
from tests.test_outbound_images import Gateway
from tests.test_workspace_images import raster


class WorkspaceImageMmsTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(importlib.util.find_spec("aiohttp"), "aiohttp extra is unavailable")
    async def test_png_exact_submission_get_head_and_safe_diagnostics(self):
        from aiohttp import ClientSession
        data = raster("PNG")
        gateway = Gateway()
        store = OutboundMediaStore()
        service = TwilioSmsService(TwilioSmsSettings(
            "AC123", "SECRET", "+15550000001", "+15550000002",
            "https://public.example/sms", "127.0.0.1", 0, "/sms",
            "https://public.example"), lambda _: None, gateway=gateway, outbound_media=store)
        with self.assertLogs(level="INFO") as logs:
            await service.start()
            try:
                evidence = await service.deliver_image(OutboundImage(data, "image/png", ""))
                self.assertEqual(evidence.status, "accepted")
                self.assertEqual(len(gateway.calls), 1)
                path = gateway.calls[0]["media_url"].removeprefix("https://public.example")
                token = path.rsplit("/", 1)[1].removesuffix(".png")
                self.assertEqual(store.get(token), data)
                self.assertEqual(store.get_image(token).media_type, "image/png")
                site = next(iter(service._runner.sites))
                port = site._server.sockets[0].getsockname()[1]
                async with ClientSession() as client:
                    for method in ("GET", "HEAD"):
                        async with client.request(method, f"http://127.0.0.1:{port}{path}") as result:
                            self.assertEqual(result.status, 200)
                            self.assertEqual(result.headers["Content-Type"], "image/png")
                            self.assertEqual(result.headers["Content-Length"], str(len(data)))
                            self.assertEqual(await result.read(), data if method == "GET" else b"")
            finally:
                await service.stop()
        self.assertEqual(store.retained_count, 0)
        diagnostics = "\n".join(logs.output)
        for hidden in (token, path, "SECRET", repr(data)):
            self.assertNotIn(hidden, diagnostics)
