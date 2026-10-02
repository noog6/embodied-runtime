import json
import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import (
    INSPECT_SELF_TOOL, OBSERVE_SCENE_TOOL, PUBLISH_FINDING_TOOL,
    REPORT_JOB_OUTCOME_TOOL,
    SEARCH_FINDINGS_TOOL, JOB_OUTCOME_EVALUATION_REQUEST,
    ApplicationOptions, RobotApplication,
)
from embodied_runtime.cognition import (
    CognitionToolCall, InitiativeAcquisitionOutcome, TextCognitionBackend,
)
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    FilesystemJobWorkspaceStore, JobRunStatus, SQLiteJobStore,
)
from embodied_runtime.memory import SQLiteMemoryStore
from embodied_runtime.perception import VisualPerceptionBackend, VisualPerceptionResult
from embodied_runtime.profile import RobotProfile
from embodied_runtime.sensing.camera import CameraBackend, CameraFrame
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


class Camera(CameraBackend):
    identifier = "finding-camera"
    is_physical = False

    def __init__(self): self.running = False
    @property
    def is_running(self): return self.running
    def start(self): self.running = True
    def stop(self): self.running = False
    def capture_frame(self): return CameraFrame(b"jpeg", "image/jpeg", 1, 1, 1)


class Vision(VisualPerceptionBackend):
    identifier = "finding-vision"
    async def interpret(self, frame, focus):
        return VisualPerceptionResult(focus, "A camera scene.")


class PublishingBackend(TextCognitionBackend):
    identifier = "finding-publisher"

    def __init__(self):
        self.acquisition = None
        self.publication = None
        self.requests = []

    async def respond(self, message, *, instructions=None, tools=(), tool_executor=None,
                      refreshed_instructions=None):
        names = tuple(tool.name for tool in tools)
        self.requests.append((instructions, names))
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            await tool_executor(CognitionToolCall(REPORT_JOB_OUTCOME_TOOL.name, json.dumps({
                "disposition": "completed", "summary": "published camera finding",
                "report": None, "readiness": None, "delay_seconds": None,
                "event_type": None, "progress_update": None,
            })))
        elif self.acquisition is None and INSPECT_SELF_TOOL.name in names:
            result = await tool_executor(CognitionToolCall(
                INSPECT_SELF_TOOL.name, '{"area":"runtime"}'))
            self.acquisition = json.loads(result.output)
        elif self.publication is None and PUBLISH_FINDING_TOOL.name in names:
            result = await tool_executor(CognitionToolCall(
                PUBLISH_FINDING_TOOL.name, json.dumps({
                    "topic": "camera", "kind": "observation",
                    "claim": "Camera was available during the source run.",
                })))
            self.publication = json.loads(result.output)
        return "bounded publishing work"


class SearchingBackend(TextCognitionBackend):
    identifier = "finding-searcher"

    def __init__(self, *, complete=False):
        self.complete = complete
        self.result = None
        self.requests = []

    async def respond(self, message, *, instructions=None, tools=(), tool_executor=None,
                      refreshed_instructions=None):
        names = tuple(tool.name for tool in tools)
        self.requests.append((instructions, names))
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            disposition = "completed" if self.complete else "continue"
            await tool_executor(CognitionToolCall(REPORT_JOB_OUTCOME_TOOL.name, json.dumps({
                "disposition": disposition, "summary": "searched historical findings",
                "report": None, "readiness": None if self.complete else "ready",
                "delay_seconds": None, "event_type": None, "progress_update": None,
            })))
        elif self.result is None and SEARCH_FINDINGS_TOOL.name in names:
            result = await tool_executor(CognitionToolCall(
                SEARCH_FINDINGS_TOOL.name, json.dumps({"query": "camera"})))
            self.result = json.loads(result.output)
        return "A prior Job reported a historical, non-authoritative camera finding."


class AcquisitionPublishingBackend(PublishingBackend):
    def __init__(self, acquisition_name, acquisition_arguments, kind):
        super().__init__()
        self.acquisition_name = acquisition_name
        self.acquisition_arguments = acquisition_arguments
        self.kind = kind

    async def respond(self, message, *, instructions=None, tools=(), tool_executor=None,
                      refreshed_instructions=None):
        names = tuple(tool.name for tool in tools)
        self.requests.append((instructions, names))
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            disposition = "completed" if self.publication and self.publication.get(
                "status") == "applied" else "continue"
            await tool_executor(CognitionToolCall(REPORT_JOB_OUTCOME_TOOL.name, json.dumps({
                "disposition": disposition, "summary": "publication attempted",
                "report": None, "readiness": None if disposition == "completed" else "ready",
                "delay_seconds": None, "event_type": None, "progress_update": None,
            })))
        elif self.acquisition is None and self.acquisition_name in names:
            result = await tool_executor(CognitionToolCall(
                self.acquisition_name, json.dumps(self.acquisition_arguments)))
            self.acquisition = json.loads(result.output)
        elif self.publication is None and PUBLISH_FINDING_TOOL.name in names:
            result = await tool_executor(CognitionToolCall(
                PUBLISH_FINDING_TOOL.name, json.dumps({
                    "topic": "grounding", "kind": self.kind,
                    "claim": "A bounded grounded claim.",
                })))
            self.publication = json.loads(result.output)
        return "bounded acquisition and publication"


class ConcurrentPublishingBackend(TextCognitionBackend):
    identifier = "concurrent-finding-publisher"

    def __init__(self):
        self.acquired = set()
        self.published = {}
        self.at_publish = {1: asyncio.Event(), 2: asyncio.Event()}
        self.release = asyncio.Event()

    @staticmethod
    def run_number(instructions):
        match = re.search(r"source: JOB\d+/RUN(\d+)", instructions or "")
        return None if match is None else int(match.group(1))

    async def respond(self, message, *, instructions=None, tools=(), tool_executor=None,
                      refreshed_instructions=None):
        run = self.run_number(instructions)
        names = {tool.name for tool in tools}
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            await tool_executor(CognitionToolCall(REPORT_JOB_OUTCOME_TOOL.name, json.dumps({
                "disposition": "continue", "summary": "published concurrently",
                "report": None, "readiness": "ready", "delay_seconds": None,
                "event_type": None, "progress_update": None,
            })))
        elif run is not None and run not in self.acquired:
            capability = (INSPECT_SELF_TOOL.name if run == 1
                          else "inspect_runtime_health")
            arguments = {"area": "runtime"} if run == 1 else {}
            await tool_executor(CognitionToolCall(capability, json.dumps(arguments)))
            self.acquired.add(run)
        elif run is not None and run not in self.published and PUBLISH_FINDING_TOOL.name in names:
            self.at_publish[run].set()
            await self.release.wait()
            result = await tool_executor(CognitionToolCall(
                PUBLISH_FINDING_TOOL.name, json.dumps({
                    "topic": f"concurrent_{run}", "kind": "observation",
                    "claim": f"Claim from concurrent run {run}.",
                })))
            self.published[run] = json.loads(result.output)
        return "concurrent bounded work"


class FindingCognitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")
        self.memory_store = SQLiteMemoryStore(Path(self.temp.name) / "memory.sqlite3")
        self.workspaces = FilesystemJobWorkspaceStore(Path(self.temp.name) / "workspaces")
        self.entity = self.memory_store.create_entity("system", "Test runtime")

    def tearDown(self):
        self.store.close()
        self.memory_store.close()
        self.workspaces.close()
        self.temp.cleanup()

    def app(self, backend, **kwargs):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True,
                               initiative_goal_closure_enabled=True,
                               diagnostics_enabled=True,
                               jobs_max_concurrent_work=2),
            platform_provider=Platform(), cognition_backend=backend,
            job_store=self.store,
            persistent_memory_store=self.memory_store,
            **kwargs,
            wall_clock=lambda: datetime(2026, 10, 2, tzinfo=UTC))

    async def publish_completed(self, app, job_id):
        binding = app.start_job_run(job_id)
        before = app.working_memory.snapshot()
        memories_before = self.memory_store.list_memories_for_entity(self.entity.id)
        await app.work_current_job_once()
        self.assertEqual(app.working_memory.snapshot(), before)
        self.assertEqual(self.memory_store.list_memories_for_entity(self.entity.id),
                         memories_before)
        finding = self.store.list_findings(limit=1)[0]
        self.assertEqual(self.store.get_run(binding.run.id).status,
                         JobRunStatus.COMPLETED)
        return binding, finding

    async def test_real_job_a_publish_then_job_b_search(self):
        publisher = PublishingBackend()
        app = self.app(publisher)
        await app.start()
        job_a = self.store.create_job("Camera capability research")
        binding_a, finding = await self.publish_completed(app, job_a.id)
        self.assertEqual(publisher.publication["status"], "applied")

        searcher = SearchingBackend()
        app._cognition_backend = searcher
        job_b = self.store.create_job("Embodiment planning")
        binding_b = app.start_job_run(job_b.id)
        memory_before = app.working_memory.snapshot()
        persistent_before = self.memory_store.list_memories_for_entity(self.entity.id)
        await app.work_current_job_once()
        self.assertEqual(app.working_memory.snapshot(), memory_before)
        self.assertEqual(self.memory_store.list_memories_for_entity(self.entity.id),
                         persistent_before)
        result = searcher.result
        self.assertEqual(result["status"], "ok")
        projected = result["findings"][0]
        self.assertEqual(projected["id"], f"FIND{finding.id}")
        self.assertEqual(projected["source_job"]["id"], f"JOB{job_a.id}")
        self.assertEqual(projected["source_run"], f"RUN{binding_a.run.id}")
        self.assertEqual(projected["source_task"], str(binding_a.task.id))
        self.assertTrue(projected["source_episode"].startswith("E"))
        self.assertTrue(projected["evidence_basis"])
        self.assertEqual(projected["content_authority"],
                         "job_authored_non_authoritative")
        current = app.current_job_run
        self.assertEqual((current.job.id, current.run.id, current.task.id),
                         (job_b.id, binding_b.run.id, binding_b.task.id))
        self.assertIn("acquisitions_used: 1", searcher.requests[1][0])
        await app.stop()

    async def test_real_same_job_prior_run_search_hides_active_run(self):
        publisher = PublishingBackend()
        app = self.app(publisher)
        await app.start()
        job = self.store.create_job("Recurring camera research")
        run1, finding = await self.publish_completed(app, job.id)
        searcher = SearchingBackend()
        app._cognition_backend = searcher
        run2 = app.start_job_run(job.id)
        await app.work_current_job_once()
        ids = [item["id"] for item in searcher.result["findings"]]
        self.assertEqual(ids, [f"FIND{finding.id}"])
        self.assertEqual(searcher.result["findings"][0]["source_run"],
                         f"RUN{run1.run.id}")
        self.assertNotEqual(run1.run.id, run2.run.id)
        self.assertEqual(self.store.get_run(run2.run.id).status, JobRunStatus.RUNNING)
        await app.stop()

    async def test_real_operator_search_uses_optional_limit_and_normal_budget(self):
        publisher = PublishingBackend()
        app = self.app(publisher)
        await app.start()
        job = self.store.create_job("Camera research")
        _, finding = await self.publish_completed(app, job.id)
        searcher = SearchingBackend()
        app._cognition_backend = searcher
        before = app.working_memory.snapshot()
        persistent_before = self.memory_store.list_memories_for_entity(self.entity.id)
        answer = await app.request_cognition("What have your Jobs learned about the camera?")
        self.assertIn("historical, non-authoritative", answer)
        self.assertEqual(searcher.result["findings"][0]["id"], f"FIND{finding.id}")
        self.assertIn("search_findings", searcher.requests[0][1])
        self.assertIn("acquisitions_used: 1", searcher.requests[1][0])
        # Only the ordinary operator request/response turn is added; no synthetic Finding turn.
        self.assertEqual(len(app.working_memory.snapshot()), len(before) + 1)
        self.assertEqual(self.memory_store.list_memories_for_entity(self.entity.id),
                         persistent_before)
        await app.stop()

    async def test_projection_boundaries(self):
        app = self.app(SearchingBackend())
        await app.start()
        self.assertIn("search_findings", [tool.name for tool in app.cognition_tools()])
        self.assertNotIn("publish_finding", [tool.name for tool in app.cognition_tools()])
        app.set_goal("generic initiative")
        self.assertNotIn("publish_finding", [tool.name for tool in app.initiative_tools()])
        app.resolve_goal("cancelled")
        job = self.store.create_job("Bounded work")
        app.start_job_run(job.id)
        job_tools = [tool.name for tool in app._initiative_tools_for_episode(job_work=True)]
        self.assertIn("search_findings", job_tools)
        self.assertIn("publish_finding", job_tools)
        await app.stop()

    async def test_application_stop_before_and_after_publication(self):
        app = self.app(SearchingBackend())
        await app.start()
        evidence = (InitiativeAcquisitionOutcome(
            "inspect_self", "applied", '{"status":"applied"}'),)

        first = app.start_job_run(self.store.create_job("Stopped first").id)
        first_context = app._context_for_run(first.run.id)
        first_binding = app._validate_job_work_preconditions(first_context)
        app.finish_job_run(JobRunStatus.STOPPED, "operator stopped")
        rejected = app._execute_publish_finding(
            CognitionToolCall(PUBLISH_FINDING_TOOL.name, json.dumps({
                "topic": "race", "kind": "observation", "claim": "too late"})),
            job_binding=first_binding, expected_goal=first_binding[2], episode_id=10,
            acquisitions=evidence)
        self.assertEqual(json.loads(rejected.output)["status"], "rejected")
        self.assertEqual(self.store.list_findings(), ())

        second = app.start_job_run(self.store.create_job("Published first").id)
        second_context = app._context_for_run(second.run.id)
        second_binding = app._validate_job_work_preconditions(second_context)
        applied = app._execute_publish_finding(
            CognitionToolCall(PUBLISH_FINDING_TOOL.name, json.dumps({
                "topic": "race", "kind": "observation", "claim": "committed first"})),
            job_binding=second_binding, expected_goal=second_binding[2], episode_id=11,
            acquisitions=evidence)
        self.assertEqual(json.loads(applied.output)["status"], "applied")
        finding = self.store.list_findings()[0]
        app.finish_job_run(JobRunStatus.STOPPED, "stopped after commit")
        self.assertEqual(self.store.get_finding(finding.id), finding)
        self.assertEqual(self.store.search_findings("race"), ())
        await app.stop()

    async def test_sensor_observation_uses_sensor_provenance(self):
        backend = AcquisitionPublishingBackend(
            OBSERVE_SCENE_TOOL.name, {"focus": "camera availability"}, "observation")
        app = self.app(backend, camera_backend=Camera(),
                       visual_perception_backend=Vision())
        await app.start()
        binding, finding = await self.publish_completed(
            app, self.store.create_job("Observe camera").id)
        self.assertEqual(backend.publication["status"], "applied")
        self.assertEqual([item.evidence_class.value for item in finding.evidence_basis],
                         ["sensor_observation"])
        self.assertEqual(finding.run_id, binding.run.id)
        await app.stop()

    async def test_workspace_observation_rejects_but_synthesis_preserves_history(self):
        observation_job = self.store.create_job("Workspace observation")
        self.workspaces.write(observation_job.id, "note.txt", "create", "old prose")
        rejection = AcquisitionPublishingBackend(
            "workspace_read", {"path": "note.txt", "offset_chars": 0}, "observation")
        app = self.app(rejection, job_workspace_store=self.workspaces)
        await app.start()
        app.start_job_run(observation_job.id)
        await app.work_current_job_once()
        self.assertEqual(rejection.publication["status"], "rejected")
        self.assertEqual(self.store.list_findings(), ())
        app.finish_job_run(JobRunStatus.STOPPED, "expected rejection")

        synthesis_job = self.store.create_job("Workspace synthesis")
        self.workspaces.write(synthesis_job.id, "note.txt", "create", "old prose")
        synthesis = AcquisitionPublishingBackend(
            "workspace_read", {"path": "note.txt", "offset_chars": 0}, "synthesis")
        app._cognition_backend = synthesis
        _, finding = await self.publish_completed(app, synthesis_job.id)
        self.assertEqual(synthesis.publication["status"], "applied")
        self.assertEqual([item.evidence_class.value for item in finding.evidence_basis],
                         ["job_workspace_historical"])
        await app.stop()

    async def test_finding_synthesis_stays_historical(self):
        app = self.app(PublishingBackend())
        await app.start()
        source = self.store.create_job("Source research")
        await self.publish_completed(app, source.id)
        synthesis = AcquisitionPublishingBackend(
            SEARCH_FINDINGS_TOOL.name, {"query": "camera"}, "synthesis")
        app._cognition_backend = synthesis
        target = self.store.create_job("Historical synthesis")
        _, finding = await self.publish_completed(app, target.id)
        classes = {item.evidence_class.value for item in finding.evidence_basis}
        self.assertEqual(classes, {"historical_finding"})
        self.assertTrue(classes.isdisjoint({"runtime_inspection", "sensor_observation"}))
        await app.stop()

    async def test_two_live_jobs_publish_with_foreground_independent_provenance(self):
        backend = ConcurrentPublishingBackend()
        app = self.app(backend)
        await app.start()
        a = app.start_job_run(self.store.create_job("Concurrent A").id)
        b = app.start_job_run(self.store.create_job("Concurrent B").id)
        work_a = asyncio.create_task(app.work_job_run_once(a.run.id))
        work_b = asyncio.create_task(app.work_job_run_once(b.run.id))
        await asyncio.gather(backend.at_publish[a.run.id].wait(),
                             backend.at_publish[b.run.id].wait())
        # Deliberately make B foreground while both exact task-local executions
        # are suspended immediately before publication.
        app._foreground_job_run_id = b.run.id
        backend.release.set()
        await asyncio.gather(work_a, work_b)
        findings = {finding.run_id: finding for finding in self.store.list_findings()}
        find_a, find_b = findings[a.run.id], findings[b.run.id]
        self.assertEqual((find_a.job_id, find_a.task_id),
                         (a.job.id, str(a.task.id)))
        self.assertEqual((find_b.job_id, find_b.task_id),
                         (b.job.id, str(b.task.id)))
        self.assertNotEqual(find_a.episode_id, find_b.episode_id)
        self.assertEqual([item.capability for item in find_a.evidence_basis],
                         ["inspect_self"])
        self.assertEqual([item.capability for item in find_b.evidence_basis],
                         ["inspect_runtime_health"])
        self.assertEqual(self.store.get_run(a.run.id).job_id, a.job.id)
        self.assertEqual(self.store.get_run(b.run.id).job_id, b.job.id)
        await app.stop()


if __name__ == "__main__":
    unittest.main()
