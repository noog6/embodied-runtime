import json
import unittest

from embodied_runtime.app import RobotApplication
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


JPEG = b"\xff\xd8\xffexact-episode-frame"
SMS_DIALOGUE = InteractionContext(
    channel=InteractionChannel.REMOTE_TEXT, mode=InteractionMode.DIALOGUE,
    cadence=InteractionCadence.BOUNDED_TURN,
    initiator=InteractionInitiator.OPERATOR, response_expected=True,
)


class Platform:
    def snapshot(self):
        return snapshot()


class Camera(CameraBackend):
    identifier = "fake-camera"
    is_physical = False

    def __init__(self):
        self.running = False
        self.frames = [JPEG]
        self.captures = 0

    @property
    def is_running(self):
        return self.running

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def capture_frame(self):
        data = self.frames[min(self.captures, len(self.frames) - 1)]
        self.captures += 1
        return CameraFrame(data, "image/jpeg", 2, 1, 1_000_000_000)


class ImageSink:
    channel = InteractionChannel.REMOTE_TEXT
    image_delivery_available = True

    def __init__(self):
        self.images = []
        self.evidence = ImageDeliveryEvidence("accepted", "MM-safe", "queued")

    async def deliver_image(self, image):
        self.images.append(image)
        return self.evidence

    async def deliver(self, _message):
        raise AssertionError("ordinary delivery must not duplicate the MMS")


class CaptureThenDeliver(TextCognitionBackend):
    identifier = "capture-then-deliver"

    def __init__(self, *, deliver=True, reference=None, on_capture=None):
        self.deliver = deliver
        self.reference = reference
        self.on_capture = on_capture
        self.calls = 0
        self.results = []

    async def respond(self, _message, *, tools=(), tool_executor=None, **_kwargs):
        self.calls += 1
        if self.calls == 1 and self.reference is None:
            result = json.loads((await tool_executor(CognitionToolCall(
                "capture_camera_image", "{}"))).output)
            self.results.append(result)
            self.reference = result.get("image_ref")
            if self.on_capture:
                self.on_capture()
            return "captured"
        if self.deliver:
            result = json.loads((await tool_executor(CognitionToolCall(
                "deliver_image", json.dumps({
                    "image_ref": self.reference, "destination": "sms", "caption": "Fresh photo",
                })))).output)
            self.results.append(result)
        return "done"


class SameStageBypassBackend(TextCognitionBackend):
    identifier = "same-stage-bypass"

    def __init__(self):
        self.calls = 0
        self.results = []

    async def respond(self, _message, *, tool_executor=None, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            captured = json.loads((await tool_executor(CognitionToolCall(
                "capture_camera_image", "{}"))).output)
            attempted = json.loads((await tool_executor(CognitionToolCall(
                "deliver_image", json.dumps({
                    "image_ref": captured["image_ref"], "destination": "sms",
                    "caption": "bypass",
                })))).output)
            self.results.extend((captured, attempted))
        return "done"


class OperatorImageEpisodeTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, backend, camera, sink, catalog=None):
        routes = catalog or OperatorDeliveryRouteCatalog((OperatorDeliveryRoute(
            OperatorDeliveryDestination(
                "sms", InteractionChannel.REMOTE_TEXT, "configured operator SMS"), sink,
        ),))
        return RobotApplication(
            RobotProfile("test", "Test Robot"), VirtualHardwareBackend(),
            platform_provider=Platform(), cognition_backend=backend,
            camera_backend=camera, operator_delivery_routes=routes,
        )

    async def test_sms_voice_and_console_capture_then_send_exact_bytes_once(self):
        for interaction in (SMS_DIALOGUE, VOICE_DIALOGUE, CONSOLE_DIALOGUE):
            with self.subTest(channel=interaction.channel):
                camera, sink = Camera(), ImageSink()
                backend = CaptureThenDeliver(
                    on_capture=lambda: camera.frames.append(b"\xff\xd8\xffnewer-frame"))
                app = self.make_app(backend, camera, sink)
                await app.start()
                try:
                    await app.request_cognition("Send me a picture.", interaction=interaction)
                finally:
                    await app.stop()
                self.assertEqual(camera.captures, 1)
                self.assertEqual(len(sink.images), 1)
                self.assertEqual(sink.images[0].data, JPEG)
                self.assertEqual(backend.results[-1]["status"], "applied")

    async def test_capture_alone_sends_nothing_and_needs_no_vision_backend(self):
        camera, sink = Camera(), ImageSink()
        backend = CaptureThenDeliver(deliver=False)
        app = self.make_app(backend, camera, sink)
        await app.start()
        try:
            await app.request_cognition("Take a photo only.", interaction=CONSOLE_DIALOGUE)
        finally:
            await app.stop()
        self.assertEqual(camera.captures, 1)
        self.assertEqual(sink.images, [])
        self.assertIsNone(app._visual_perception_backend)

    async def test_unknown_and_cross_episode_reference_are_rejected(self):
        camera, sink = Camera(), ImageSink()
        unknown = CaptureThenDeliver(reference="img_unknown")
        app = self.make_app(unknown, camera, sink)
        await app.start()
        try:
            await app.request_cognition("Send unknown.", interaction=CONSOLE_DIALOGUE)
            self.assertEqual(unknown.results[-1]["status"], "rejected")
            first = CaptureThenDeliver(deliver=False)
            app._cognition_backend = first
            await app.request_cognition("Capture only.", interaction=CONSOLE_DIALOGUE)
            cross = CaptureThenDeliver(reference=first.reference)
            app._cognition_backend = cross
            await app.request_cognition("Send old.", interaction=CONSOLE_DIALOGUE)
            self.assertEqual(cross.results[-1]["reason"],
                             "image_ref is unknown, expired, or from another episode")
        finally:
            await app.stop()
        self.assertEqual(sink.images, [])

    async def test_route_change_after_capture_rejects_delivery(self):
        camera, sink = Camera(), ImageSink()
        app = None

        def remove_route():
            app._operator_delivery_routes = OperatorDeliveryRouteCatalog()

        backend = CaptureThenDeliver(on_capture=remove_route)
        app = self.make_app(backend, camera, sink)
        await app.start()
        try:
            await app.request_cognition("Send photo.", interaction=VOICE_DIALOGUE)
        finally:
            await app.stop()
        self.assertEqual(backend.results[-1]["status"], "rejected")
        self.assertEqual(sink.images, [])

    async def test_uncertain_submission_is_not_reported_as_applied(self):
        camera, sink = Camera(), ImageSink()
        sink.evidence = ImageDeliveryEvidence(
            "uncertain", reason="raw provider detail must be ignored")
        backend = CaptureThenDeliver()
        app = self.make_app(backend, camera, sink)
        await app.start()
        try:
            await app.request_cognition("Send photo.", interaction=SMS_DIALOGUE)
        finally:
            await app.stop()
        self.assertEqual(backend.results[-1], {
            "destination": "sms", "handset_receipt": "unconfirmed",
            "reason": "submission_uncertain", "status": "uncertain",
            "submission": "uncertain",
        })

    async def test_same_stage_attempt_cannot_bypass_episode_capability_limit(self):
        camera, sink = Camera(), ImageSink()
        backend = SameStageBypassBackend()
        app = self.make_app(backend, camera, sink)
        await app.start()
        try:
            await app.request_cognition("Send photo.", interaction=CONSOLE_DIALOGUE)
        finally:
            await app.stop()
        self.assertEqual(backend.results[0]["status"], "applied")
        self.assertEqual(backend.results[1]["status"], "rejected")
        self.assertEqual(sink.images, [])
