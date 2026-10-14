"""Image workflows through the real finite operator and Job execution paths."""

import asyncio
import base64
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from embodied_runtime.app import (
    ApplicationOptions, JOB_OUTCOME_EVALUATION_REQUEST, RobotApplication,
    JOB_WORKSPACE_LOAD_IMAGE_TOOL, JOB_WORKSPACE_SAVE_IMAGE_TOOL,
)
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.interaction import (
    InteractionChannel, OperatorDeliveryDestination, OperatorDeliveryRoute,
    OperatorDeliveryRouteCatalog,
)
from embodied_runtime.jobs import FilesystemJobWorkspaceStore, SQLiteJobStore, WorkspaceDurabilityError
from embodied_runtime.outbound_images import RetainedImage
from embodied_runtime.profile import RobotProfile
from embodied_runtime.perception.factory import create_visual_perception_backend
from embodied_runtime.perception.visual import VisualPerceptionBackend, VisualPerceptionResult
from embodied_runtime.cli import parse_launch_arguments, validate_launch_dependencies
from embodied_runtime.resources import ResourceOwner
from embodied_runtime.app import CAMERA_RESOURCE
from tests.test_operator_image_delivery import Camera, ImageSink, Platform
from tests.test_openai_compatible import FakeCompletions, client, response
from tests.test_portable_visual_perception import CONFIG, direct_client, direct_response
from tests.test_workspace_images import CAPTURED, STORED, provenance, raster


class Script(TextCognitionBackend):
    identifier = "image-workflow-test"

    def __init__(self, steps, after=None, duplicate=False):
        self.steps = list(steps)
        self.after = after
        self.duplicate = duplicate
        self.reference = None
        self.results, self.requests, self.executors = [], [], []

    async def respond(self, message, *, tools=(), tool_executor=None, **kwargs):
        self.requests.append(tuple(tool.name for tool in tools))
        self.executors.append(tool_executor)
        if message == JOB_OUTCOME_EVALUATION_REQUEST or not self.steps:
            return "done"
        name, arguments = self.steps.pop(0)
        arguments = {key: self.reference if value == "$ref" else value for key, value in arguments.items()}
        result = json.loads((await tool_executor(CognitionToolCall(name, json.dumps(arguments)))).output)
        self.results.append(result)
        self.reference = result.get("image_ref", self.reference)
        if self.duplicate and name == "workspace_save_image":
            repeated = await tool_executor(CognitionToolCall(name, json.dumps(arguments)))
            self.results.append(json.loads(repeated.output))
        if self.after:
            self.after(name, result)
        return "stage complete"


class Vision(VisualPerceptionBackend):
    identifier = "fake-vision"

    def __init__(self):
        self.frames = []
        self.waiting = asyncio.Event()
        self.block = False
        self.failure = None

    async def interpret(self, frame, focus):
        self.frames.append(frame)
        if self.failure:
            raise self.failure
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()
        return VisualPerceptionResult(focus, "A possibly red object; identity uncertain.")


class ImageWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspaces"
        self.jobs = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")
        self.store = FilesystemJobWorkspaceStore(self.root)
        self.job = self.jobs.create_job("Image validation")
        self.a, self.b = raster(), raster("PNG", "blue")
        self.sink = ImageSink()
        self.vision = Vision()
        self.app = None

    async def asyncTearDown(self):
        if self.app is not None:
            await self.app.stop()
        self.store.close(); self.jobs.close(); self.temp.cleanup()

    def saved(self, data=None, capture=CAPTURED):
        self.store.save_image(self.job.id, "photos/frame", "upsert",
                              self.a if data is None else data, provenance(self.job.id, capture))

    async def start(self, backend, *, camera=None, vision=None, job_work=False):
        self.app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=job_work),
            platform_provider=Platform(), cognition_backend=backend,
            camera_backend=camera, visual_perception_backend=vision or self.vision,
            job_store=self.jobs, job_workspace_store=self.store,
            wall_clock=lambda: STORED,
            operator_delivery_routes=OperatorDeliveryRouteCatalog((OperatorDeliveryRoute(
                OperatorDeliveryDestination("sms", InteractionChannel.REMOTE_TEXT,
                                            "configured operator SMS"), self.sink),)),
        )
        await self.app.start()
        return self.app

    def load(self, *, job_work=False):
        return ("workspace_load_image", {"path": "photos/frame", **({} if job_work else {"job": self.job.name})})

    def interpret(self):
        return ("interpret_image", {"image_ref": "$ref", "focus": "Describe visible colors and uncertainty"})

    def deliver(self):
        return ("deliver_image", {"image_ref": "$ref", "destination": "sms", "caption": ""})

    async def test_capture_save_uses_one_frame_no_vision_and_one_effect(self):
        camera = Camera(); camera.frames = [self.a, self.b]
        script = Script([
            ("capture_camera_image", {}),
            ("workspace_save_image", {"job": self.job.name, "path": "photos/frame",
                                      "mode": "create", "image_ref": "$ref"}),
        ], duplicate=True)
        app = await self.start(script, camera=camera)
        await app.request_cognition("Capture and save one image to Image validation photos/frame")
        self.assertEqual(camera.captures, 1)
        self.assertEqual(self.vision.frames, [])
        image = self.store.load_image(self.job.id, "photos/frame")
        self.assertEqual(image.data, self.a)
        self.assertEqual(image.provenance.source_identity, camera.identifier)
        self.assertEqual(image.provenance.captured_at, datetime.fromtimestamp(1, UTC))
        self.assertEqual(script.results[1]["status"], "applied")
        self.assertEqual(script.results[2]["status"], "rejected")
        self.assertNotIn("workspace_save_image", script.requests[0])
        self.assertIn("workspace_save_image", script.requests[1])

    async def test_load_interpret_configured_gemini_fallback_without_camera_retains_a(self):
        self.saved()
        primary = FakeCompletions(ConnectionError("SECRET provider body /physical/path"))
        secondary = FakeCompletions(direct_response())
        chain = create_visual_perception_backend("configured", CONFIG,
                                                 clients=(client(primary), direct_client(secondary)))
        script = Script([self.load(), self.interpret()],
                        after=lambda name, _: self.saved(self.b) if name == "workspace_load_image" else None)
        app = await self.start(script, vision=chain)
        await app.request_cognition("Interpret the saved image")
        self.assertIsNone(app.camera_backend)
        self.assertEqual(len(primary.requests), 1); self.assertEqual(len(secondary.requests), 1)
        for url in (primary.requests[0]["messages"][1]["content"][1]["image_url"]["url"],
                    secondary.requests[0]["input"][0]["content"][1]["image_url"]):
            self.assertEqual(base64.b64decode(url.split(",")[1]), self.a)
        self.assertIn("historical", primary.requests[0]["messages"][0]["content"])
        self.assertIn(CAPTURED.isoformat(), secondary.requests[0]["instructions"])
        result = script.results[-1]
        self.assertEqual(result["captured_at"], CAPTURED.isoformat())
        self.assertEqual(result["interpreted_at"], STORED.isoformat())
        self.assertEqual(result["content_version"], hashlib.sha256(self.a).hexdigest())
        self.assertEqual(app.observability.snapshot()["metrics"]["vision_fallbacks"], 1)
        self.assertEqual(self.store.load_image(self.job.id, "photos/frame").data, self.b)
        self.assertNotIn("interpret_image", script.requests[0])
        self.assertIn("interpret_image", script.requests[1])
        self.assertNotIn("interpret_image", script.requests[2])  # both acquisition slots consumed

    async def test_load_deliver_exact_a_no_vision_and_no_camera(self):
        self.saved()
        script = Script([self.load(), self.deliver()],
                        after=lambda name, _: self.saved(self.b) if name == "workspace_load_image" else None)
        app = await self.start(script)
        await app.request_cognition("Send the saved image through MMS")
        self.assertEqual(len(self.sink.images), 1)
        self.assertEqual(self.sink.images[0].data, self.a)
        self.assertEqual(self.vision.frames, [])
        self.assertEqual(script.results[-1]["submission"], "accepted")
        self.assertEqual(script.results[-1]["handset_receipt"], "unconfirmed")

    async def test_interpretation_unknown_capture_age_and_diagnostics_are_safe(self):
        self.saved(capture=None)
        script = Script([self.load(), self.interpret()])
        app = await self.start(script)
        with self.assertLogs(level="INFO") as logs:
            await app.request_cognition("Interpret saved image")
        self.assertIsNone(script.results[0]["captured_at"])
        self.assertIsNone(script.results[1]["captured_at"])
        self.assertEqual(script.results[1]["capture_age"], "unknown")
        self.assertIsNone(self.vision.frames[0].captured_at)
        diagnostics = "\n".join(logs.output)
        for forbidden in (str(self.root), base64.b64encode(self.a).decode(), repr(self.a), script.reference):
            self.assertNotIn(forbidden, diagnostics)
        # Historical interpretation does not create a fresh WorkingMemory observation.
        self.assertFalse(any(observation.kind == "visual"
                             for turn in app.working_memory.snapshot()
                             for observation in turn.observations))

    async def test_expired_foreign_reference_and_retained_executor_cannot_replay(self):
        self.saved()
        script = Script([self.load(), self.deliver()])
        app = await self.start(script)
        await app.request_cognition("Send saved image")
        reference = script.reference
        rejected = await script.executors[0](CognitionToolCall("workspace_load_image", json.dumps(self.load()[1])))
        self.assertEqual(json.loads(rejected.output)["status"], "rejected")
        script.steps = [("deliver_image", {"image_ref": reference, "destination": "sms", "caption": ""})]
        await app.request_cognition("Send same old reference")
        self.assertEqual(script.results[-1]["status"], "rejected")
        self.assertEqual(len(self.sink.images), 1)
        foreign = RetainedImage(reference, 99, self.a, "image/jpeg", 8, 6, "camera", CAPTURED)
        result = await app._execute_image_interpretation(
            CognitionToolCall("interpret_image", json.dumps({"image_ref": reference, "focus": "colors"})),
            {reference: foreign}, 100)
        self.assertEqual(json.loads(result.output)["status"], "rejected")
        self.assertEqual(self.vision.frames, [])

    async def test_job_capture_save_and_job_origin_no_unsolicited_delivery(self):
        camera = Camera(); camera.frames = [self.a]
        script = Script([("capture_camera_image", {}), ("workspace_save_image", {
            "path": "photos/frame", "mode": "create", "image_ref": "$ref"})])
        app = await self.start(script, camera=camera, job_work=True)
        binding = app.start_job_run(self.job.id)
        await app.work_current_job_once()
        self.assertEqual(camera.captures, 1)
        self.assertEqual(script.results[-1]["status"], "applied")
        self.assertEqual(self.store.load_image(self.job.id, "photos/frame").provenance.job_run_id, binding.run.id)
        self.assertTrue(all("deliver_image" not in request for request in script.requests))
        self.assertEqual(self.vision.frames, [])
        for tool in (JOB_WORKSPACE_LOAD_IMAGE_TOOL, JOB_WORKSPACE_SAVE_IMAGE_TOOL):
            self.assertNotIn("job", tool.parameters["properties"])

    async def test_job_load_interpret_uses_two_acquisitions_then_text_effect(self):
        self.saved()
        script = Script([self.load(job_work=True), self.interpret(), ("workspace_write", {
            "path": "description.txt", "mode": "create", "content": "Separate authored description"})])
        app = await self.start(script, job_work=True)
        app.start_job_run(self.job.id)
        await app.work_current_job_once()
        self.assertEqual([item["status"] for item in script.results], ["ok", "applied", "applied"])
        self.assertEqual(self.vision.frames[0].data, self.a)
        self.assertNotIn("workspace_load_image", script.requests[2])
        self.assertNotIn("interpret_image", script.requests[2])
        self.assertEqual(self.store.read(self.job.id, "description.txt").content, "Separate authored description")

    async def test_cross_job_selector_foreign_snapshot_and_stale_binding_fail_closed(self):
        self.saved()
        other = self.jobs.create_job("Other")
        script = Script([])
        app = await self.start(script, job_work=True)
        binding = app.start_job_run(self.job.id)
        bound = (binding, app._current_task_binding, app.active_goal)
        refs = {}
        result = app._execute_workspace_image(CognitionToolCall("workspace_load_image", json.dumps({
            "job": other.name, "path": "photos/frame"})), refs, 1, job_binding=bound,
            expected_goal=app.active_goal, job_work=True)
        self.assertEqual(json.loads(result.output)["status"], "rejected")
        refs["foreign"] = RetainedImage("foreign", 1, self.a, "image/jpeg", 8, 6,
                                         "camera", CAPTURED, job_id=other.id)
        call = CognitionToolCall("workspace_save_image", json.dumps({
            "path": "foreign", "mode": "create", "image_ref": "foreign"}))
        result = app._execute_workspace_image(call, refs, 1, job_binding=bound,
                                             expected_goal=app.active_goal, job_work=True)
        self.assertEqual(json.loads(result.output)["reason"], "foreign_image_reference")
        app.pause_task()
        result = app._execute_workspace_image(call, refs, 1, job_binding=bound,
                                             expected_goal=bound[2], job_work=True)
        self.assertEqual(json.loads(result.output)["reason"], "stale_job_work_binding")

    async def test_indeterminate_save_is_reported_once_without_replay(self):
        camera = Camera(); camera.frames = [self.a]
        script = Script([("capture_camera_image", {}), ("workspace_save_image", {
            "job": self.job.name, "path": "photos/frame", "mode": "upsert", "image_ref": "$ref"})])
        app = await self.start(script, camera=camera)
        with patch.object(self.store, "save_image", side_effect=WorkspaceDurabilityError("PRIVATE")) as save:
            await app.request_cognition("Save image")
        self.assertEqual(save.call_count, 1)
        self.assertEqual(script.results[-1]["status"], "indeterminate")
        self.assertTrue(script.results[-1]["published"])
        self.assertFalse(script.results[-1]["durability_confirmed"])

    async def test_cancellation_interrupts_vision_and_clears_retained_references(self):
        self.saved(); self.vision.block = True
        script = Script([self.load(), self.interpret(), self.deliver()])
        app = await self.start(script)
        task = asyncio.create_task(app.request_cognition("Interpret saved image"))
        await self.vision.waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.sink.images, [])
        self.assertEqual(len(self.vision.frames), 1)
        self.vision.block = False
        script.steps = [("deliver_image", {"image_ref": script.reference, "destination": "sms", "caption": ""})]
        await app.request_cognition("Send expired snapshot")
        self.assertEqual(script.results[-1]["status"], "rejected")

    async def test_camera_resource_busy_does_not_capture_save_or_interpret(self):
        camera = Camera(); camera.frames = [self.a]
        script = Script([("capture_camera_image", {})])
        app = await self.start(script, camera=camera)
        lease = app.resources.acquire(CAMERA_RESOURCE, ResourceOwner("test", "holder"))
        try:
            await app.request_cognition("Save a camera image")
        finally:
            app.resources.release(lease)
        self.assertEqual(script.results[0]["reason"], "camera_busy")
        self.assertEqual(camera.captures, 0)
        self.assertEqual(self.vision.frames, [])

    async def test_saved_interpretation_configuration_does_not_require_camera(self):
        launch = parse_launch_arguments(["--vision", "openai-responses", "--cognition", "openai-responses"])[1]
        validate_launch_dependencies(launch)

    async def test_third_acquisition_is_rejected_without_camera_access(self):
        self.saved()
        camera = Camera(); camera.frames = [self.a]
        script = Script([self.load(), self.interpret(), ("capture_camera_image", {})])
        app = await self.start(script, camera=camera)
        await app.request_cognition("Interpret saved image then capture again")
        self.assertEqual(script.results[-1]["status"], "rejected")
        self.assertEqual(camera.captures, 0)
        self.assertEqual(len(self.vision.frames), 1)

    async def test_saved_image_provider_refusal_does_not_fall_back(self):
        self.saved()
        primary = FakeCompletions(response(None, finish="content_filter"))
        secondary = FakeCompletions(direct_response())
        chain = create_visual_perception_backend("configured", CONFIG,
                                                 clients=(client(primary), direct_client(secondary)))
        script = Script([self.load(), self.interpret()])
        app = await self.start(script, vision=chain)
        await app.request_cognition("Interpret saved image")
        self.assertEqual(script.results[-1]["reason"], "refused")
        self.assertEqual(len(primary.requests), 1)
        self.assertEqual(secondary.requests, [])

    async def test_provider_cancellation_on_snapshot_never_calls_fallback(self):
        self.saved()
        entered = asyncio.Event()
        async def stall():
            entered.set()
            await asyncio.Event().wait()
        primary = FakeCompletions(stall)
        secondary = FakeCompletions(direct_response())
        chain = create_visual_perception_backend("configured", CONFIG,
                                                 clients=(client(primary), direct_client(secondary)))
        script = Script([self.load(), self.interpret()])
        app = await self.start(script, vision=chain)
        task = asyncio.create_task(app.request_cognition("Interpret saved image"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(primary.requests), 1)
        self.assertEqual(secondary.requests, [])
        self.assertEqual(self.sink.images, [])

    async def test_job_binding_changed_during_interpretation_rejects_result_and_save(self):
        self.saved()
        app = await self.start(Script([]), job_work=True)
        binding = app.start_job_run(self.job.id)
        bound = (binding, app._current_task_binding, app.active_goal)
        references = {}
        result = app._execute_workspace_image(CognitionToolCall("workspace_load_image", json.dumps({
            "path": "photos/frame"})), references, 10,
            job_binding=bound, expected_goal=bound[2], job_work=True)
        reference = json.loads(result.output)["image_ref"]
        class PauseVision(VisualPerceptionBackend):
            identifier = "pause-test"
            async def interpret(self, frame, focus):
                app.pause_task()
                return VisualPerceptionResult(focus, "Non-current result")
        app._visual_perception_backend = PauseVision()
        result = await app._execute_image_interpretation(CognitionToolCall("interpret_image", json.dumps({
            "image_ref": reference, "focus": "colors"})), references, 10,
            job_binding=bound, expected_goal=bound[2])
        self.assertEqual(json.loads(result.output)["status"], "rejected")
        result = app._execute_workspace_image(CognitionToolCall("workspace_save_image", json.dumps({
            "path": "copy", "mode": "create", "image_ref": reference})), references, 10,
            job_binding=bound, expected_goal=bound[2], job_work=True)
        self.assertEqual(json.loads(result.output)["reason"], "stale_job_work_binding")
        self.assertFalse((self.root / f"JOB{self.job.id}" / "copy").exists())

    async def test_capture_save_restart_then_interpret_later_episode(self):
        camera = Camera(); camera.frames = [self.a]
        script = Script([("capture_camera_image", {}), ("workspace_save_image", {
            "job": self.job.name, "path": "photos/frame", "mode": "create", "image_ref": "$ref"})])
        app = await self.start(script, camera=camera)
        await app.request_cognition("Capture and save")
        old_ref = script.reference
        await app.stop()
        self.jobs = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")
        self.store = FilesystemJobWorkspaceStore(self.root)
        later = Script([self.load(), self.interpret()])
        app = await self.start(later)
        await app.request_cognition("Interpret the saved image in a later episode")
        self.assertEqual(self.vision.frames[0].data, self.a)
        self.assertNotEqual(later.reference, old_ref)
        self.assertEqual(later.results[1]["captured_at"], datetime.fromtimestamp(1, UTC).isoformat())
        self.assertEqual(camera.captures, 1)
        self.assertIsNone(app.camera_backend)

    async def test_gemini_success_unknown_age_prompt_and_no_fallback(self):
        self.saved(capture=None)
        primary = FakeCompletions(response("Possibly red; uncertain."))
        secondary = FakeCompletions(direct_response())
        chain = create_visual_perception_backend("configured", CONFIG,
                                                 clients=(client(primary), direct_client(secondary)))
        script = Script([self.load(), self.interpret()])
        app = await self.start(script, vision=chain)
        await app.request_cognition("Interpret unknown-age saved image")
        self.assertEqual(script.results[-1]["status"], "applied")
        self.assertIn("Capture time: unknown", primary.requests[0]["messages"][0]["content"])
        self.assertEqual(secondary.requests, [])
        self.assertEqual(app.observability.snapshot()["metrics"]["vision_provider_requests"], 1)

    async def test_saved_interpretation_honors_total_deadline(self):
        from dataclasses import replace
        self.saved()
        async def stall():
            await asyncio.Event().wait()
        primary = FakeCompletions(stall)
        secondary = FakeCompletions(direct_response())
        config = replace(CONFIG, request_timeout_seconds=.01)
        chain = create_visual_perception_backend("configured", config,
                                                 clients=(client(primary), direct_client(secondary)))
        script = Script([self.load(), self.interpret()])
        app = await self.start(script, vision=chain)
        await asyncio.wait_for(app.request_cognition("Interpret saved image"), .5)
        self.assertEqual(script.results[-1]["reason"], "all_backends_failed")
        self.assertEqual(len(primary.requests), 1)
        self.assertEqual(secondary.requests, [])

    async def test_unused_operator_and_job_callbacks_expire_without_late_capture(self):
        camera = Camera(); camera.frames = [self.a]
        script = Script([])
        app = await self.start(script, camera=camera, job_work=True)
        await app.request_cognition("No image operation requested")
        result = await script.executors[0](CognitionToolCall("capture_camera_image", "{}"))
        self.assertEqual(json.loads(result.output)["status"], "rejected")
        app.start_job_run(self.job.id)
        script.executors.clear()
        await app.work_current_job_once()
        result = await script.executors[0](CognitionToolCall("capture_camera_image", "{}"))
        self.assertEqual(json.loads(result.output)["status"], "rejected")
        self.assertEqual(camera.captures, 0)
        self.assertFalse((self.root / f"JOB{self.job.id}").exists())
