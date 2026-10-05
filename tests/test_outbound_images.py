import json
from datetime import UTC, datetime
import unittest

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.interaction import (
    CONSOLE_DIALOGUE, VOICE_DIALOGUE, InteractionCadence, InteractionChannel,
    InteractionContext, InteractionInitiator, InteractionMode,
    OperatorDeliveryDestination, OperatorDeliveryRoute, OperatorDeliveryRouteCatalog,
)
from embodied_runtime.outbound_images import ImageDeliveryEvidence
from embodied_runtime.profile import RobotProfile
from embodied_runtime.sensing.camera import CameraBackend, CameraFrame
from tests.test_platform import snapshot

JPEG = b"\xff\xd8\xffcaptured-once\xff\xd9"


class Platform:
    def snapshot(self): return snapshot()


class Camera(CameraBackend):
    identifier = "fake-camera"
    is_physical = False
    def __init__(self): self.running, self.captures = False, 0
    @property
    def is_running(self): return self.running
    def start(self): self.running = True
    def stop(self): self.running = False
    def capture_frame(self):
        self.captures += 1
        return CameraFrame(JPEG, "image/jpeg", 4, 3, 1_700_000_000_000_000_000)


class ImageSink:
    channel = InteractionChannel.REMOTE_TEXT
    image_delivery_available = True
    def __init__(self): self.images = []
    async def deliver(self, message): raise AssertionError("text not expected")
    async def deliver_image(self, image, caption):
        self.images.append((image, caption))
        return ImageDeliveryEvidence("accepted", "queued", "SM-bounded")


class Backend(TextCognitionBackend):
    identifier = "image-test"
    def __init__(self, send=True):
        self.step, self.send, self.results, self.vision_offered = 0, send, [], False
    async def respond(self, message, *, tools=(), tool_executor=None, **kwargs):
        self.vision_offered |= "observe_scene" in [tool.name for tool in tools]
        if self.step == 0:
            call = CognitionToolCall("capture_camera_image", "{}")
        elif self.step == 1 and self.send:
            call = CognitionToolCall("deliver_image", json.dumps({
                "image_ref": self.results[0]["image_ref"], "destination": "sms",
                "caption": "Fresh camera photo.",
            }))
        else:
            return "Done."
        self.step += 1
        result = json.loads((await tool_executor(call)).output)
        self.results.append(result)
        return "Working."


class OutboundImageEpisodeTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, interaction, *, send=True):
        camera, sink, backend = Camera(), ImageSink(), Backend(send)
        routes = OperatorDeliveryRouteCatalog((OperatorDeliveryRoute(
            OperatorDeliveryDestination("sms", InteractionChannel.REMOTE_TEXT,
                                        "configured operator MMS"), sink),))
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(), platform_provider=Platform(), camera_backend=camera,
            cognition_backend=backend, operator_delivery_routes=routes,
            wall_clock=lambda: datetime(2026, 10, 5, tzinfo=UTC))
        await app.start()
        try:
            await app.request_cognition("Send me a picture.", interaction=interaction)
        finally:
            await app.stop()
        return camera, sink, backend

    async def test_sms_voice_and_console_send_exact_capture_without_vision(self):
        sms = InteractionContext(
            InteractionChannel.REMOTE_TEXT, InteractionMode.DIALOGUE,
            InteractionInitiator.OPERATOR, True, InteractionCadence.BOUNDED_TURN)
        for interaction in (sms, VOICE_DIALOGUE, CONSOLE_DIALOGUE):
            camera, sink, backend = await self._run(interaction)
            self.assertEqual((camera.captures, len(sink.images)), (1, 1))
            self.assertEqual(sink.images[0][0].data, JPEG)
            self.assertFalse(backend.vision_offered)
            self.assertEqual(backend.results[1]["provider_status"], "queued")
            self.assertFalse(backend.results[1]["handset_delivery_confirmed"])

    async def test_capture_alone_does_not_send(self):
        camera, sink, backend = await self._run(CONSOLE_DIALOGUE, send=False)
        self.assertEqual(camera.captures, 1)
        self.assertEqual(sink.images, [])
        self.assertEqual(backend.results[0]["status"], "acquired")
