import json
import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from embodied_runtime.app import (
    INSPECT_SELF_TOOL, OBSERVE_SCENE_TOOL, PUBLISH_FINDING_TOOL,
    REPORT_JOB_OUTCOME_TOOL,
    SEARCH_FINDINGS_TOOL, JOB_OUTCOME_EVALUATION_REQUEST,
    ApplicationOptions, RobotApplication,
)
from embodied_runtime.attention import AttentionStimulus
from embodied_runtime.cognition import (
    CognitionToolCall, InitiativeAcquisitionOutcome, TextCognitionBackend,
)
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    FindingEvidence, FindingEvidenceClass, FindingKind,
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


class PassiveBackend(TextCognitionBackend):
    identifier = "passive"
    def __init__(self): self.requests = []
    async def respond(self, message, *, instructions=None, tools=(), tool_executor=None,
                      refreshed_instructions=None):
        self.requests.append((instructions, tuple(tool.name for tool in tools)))
        return "grounded answer"


class SnapshotBackend(TextCognitionBackend):
    identifier = "snapshot"
    def __init__(self):
        self.initial = None
        self.refreshed = None
        self.requests = []
        self.acquired = asyncio.Event()
        self.release = asyncio.Event()

    async def respond(self, message, *, instructions=None, tools=(), tool_executor=None,
                      refreshed_instructions=None):
        self.requests.append(instructions)
        if self.initial is None:
            self.initial = instructions
            await tool_executor(CognitionToolCall(INSPECT_SELF_TOOL.name,
                                                  '{"area":"runtime"}'))
            self.acquired.set()
            await self.release.wait()
            self.refreshed = refreshed_instructions()
            return "inspected"
        return "comparison complete"


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
                               jobs_max_concurrent_work=2,
                               findings_context_selection_enabled=True),
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
        self.assertIn("Selected historical context", searcher.requests[0][0])
        self.assertIn("acquisitions_used: 0", searcher.requests[0][0])
        self.assertIn("acquisitions_used: 1", searcher.requests[1][0])
        # Only the ordinary operator request/response turn is added; no synthetic Finding turn.
        self.assertEqual(len(app.working_memory.snapshot()), len(before) + 1)
        self.assertEqual(self.memory_store.list_memories_for_entity(self.entity.id),
                         persistent_before)
        await app.stop()

    async def test_operator_initial_request_contains_context_without_acquisition(self):
        evidence = (FindingEvidence(1, "inspect_self", "applied",
                    FindingEvidenceClass.RUNTIME_INSPECTION),)
        ids = []
        for index in range(2):
            job = self.store.create_job(f"Capability research {index}")
            run = self.store.create_run(job.id)
            self.store.transition_run(run.id, JobRunStatus.RUNNING)
            finding = self.store.create_finding(
                job.id, run.id, f"task-{index}", index + 1, "capability",
                FindingKind.SYNTHESIS, f"Capability claim {index}", evidence)
            self.store.transition_run(run.id, JobRunStatus.COMPLETED)
            ids.append(f"FIND{finding.id}")
        backend = PassiveBackend()
        app = self.app(backend)
        await app.start()
        working_before = app.working_memory.snapshot()
        persistent_before = self.memory_store.list_memories_for_entity(self.entity.id)
        self.assertEqual(await app.request_cognition(
            "What have your Jobs learned about your capabilities?"), "grounded answer")
        self.assertEqual(len(backend.requests), 1)  # no selector provider call
        instructions, tools = backend.requests[0]
        self.assertIn("Selected historical context", instructions)
        for finding_id in ids:
            self.assertIn(finding_id, instructions)
        self.assertIn("acquisitions_used: 0", instructions)
        self.assertIn("search_findings", tools)
        self.assertEqual(len(app.working_memory.snapshot()), len(working_before) + 1)
        self.assertEqual(self.memory_store.list_memories_for_entity(self.entity.id),
                         persistent_before)
        await app.stop()

    async def test_current_state_request_does_not_inject_historical_context(self):
        publisher = PublishingBackend()
        app = self.app(publisher)
        await app.start()
        await self.publish_completed(app, self.store.create_job("Voltage capability").id)
        backend = PassiveBackend()
        app._cognition_backend = backend
        await app.request_cognition("What's your current voltage right now?")
        self.assertNotIn("Selected historical context", backend.requests[0][0])
        await app.stop()

    async def test_selection_snapshot_excludes_mid_episode_finding(self):
        evidence = (FindingEvidence(1, "inspect_self", "applied",
                    FindingEvidenceClass.RUNTIME_INSPECTION),)
        job1 = self.store.create_job("Camera baseline")
        run1 = self.store.create_run(job1.id)
        self.store.transition_run(run1.id, JobRunStatus.RUNNING)
        find1 = self.store.create_finding(
            job1.id, run1.id, "task-1", 1, "camera", FindingKind.SYNTHESIS,
            "First camera baseline.", evidence)
        self.store.transition_run(run1.id, JobRunStatus.COMPLETED)
        backend = SnapshotBackend()
        app = self.app(backend)
        await app.start()
        job2 = self.store.create_job("Camera update")
        run2 = self.store.create_run(job2.id)
        self.store.transition_run(run2.id, JobRunStatus.RUNNING)
        find2 = self.store.create_finding(
            job2.id, run2.id, "task-2", 2, "camera", FindingKind.SYNTHESIS,
            "Second camera baseline.", evidence)
        search = Mock(wraps=self.store.search_findings)
        self.store.search_findings = search
        task = asyncio.create_task(app.request_cognition(
            "Has your camera changed since the last review?"))
        await backend.acquired.wait()
        self.assertIn(f"FIND{find1.id}", backend.initial)
        self.assertNotIn(f"FIND{find2.id}", backend.initial)
        self.store.transition_run(run2.id, JobRunStatus.COMPLETED)
        backend.release.set()
        self.assertEqual(await task, "comparison complete")
        self.assertIn("acquisitions_used: 1", backend.refreshed)
        self.assertIn(f"FIND{find1.id}", backend.refreshed)
        self.assertNotIn(f"FIND{find2.id}", backend.refreshed)
        initial_context = backend.initial.split("Selected historical context", 1)[1].split(
            "\n\nWorking memory", 1)[0]
        refreshed_context = backend.refreshed.split("Selected historical context", 1)[1].split(
            "\n\nWorking memory", 1)[0]
        self.assertEqual(initial_context, refreshed_context)
        self.assertEqual(search.call_count, 1)
        later = PassiveBackend()
        app._cognition_backend = later
        await app.request_cognition("Has your camera changed since the last review?")
        self.assertIn(f"FIND{find2.id}", later.requests[0][0])
        self.assertEqual(search.call_count, 2)
        await app.stop()

    async def test_librarian_search_and_projection_fail_open(self):
        app = self.app(PassiveBackend())
        await app.start()
        original_search = self.store.search_findings
        original_projection = app._finding_projection
        for failure in ("search", "projection"):
            with self.subTest(failure=failure):
                backend = PassiveBackend()
                app._cognition_backend = backend
                if failure == "search":
                    self.store.search_findings = Mock(side_effect=RuntimeError("injected"))
                    app._finding_projection = original_projection
                else:
                    self.store.search_findings = Mock(return_value=(object(),))
                    app._finding_projection = Mock(side_effect=ValueError("injected"))
                before = app.working_memory.snapshot()
                with self.assertLogs("embodied_runtime.app", level="WARNING") as logs:
                    answer = await app.request_cognition(
                        "What have your Jobs learned about capabilities?")
                self.assertEqual(answer, "grounded answer")
                self.assertIn("status=failed", "\n".join(logs.output))
                self.assertNotIn("Selected historical context", backend.requests[0][0])
                self.assertIn("acquisitions_used: 0", backend.requests[0][0])
                self.assertEqual(len(app.working_memory.snapshot()), len(before) + 1)
                self.store.search_findings = original_search
        app._finding_projection = original_projection
        await app.stop()

    async def test_automatic_context_is_absent_from_non_operator_cognition(self):
        backend = PassiveBackend()
        app = self.app(backend)
        await app.start()
        job = self.store.create_job("Camera historical work")
        app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertTrue(backend.requests)
        self.assertTrue(all("Selected historical context" not in instructions
                            for instructions, _ in backend.requests))
        app.finish_job_run(JobRunStatus.STOPPED, "test cleanup")
        backend.requests.clear()
        app.set_goal("consider camera history")
        await app._request_initiative(AttentionStimulus(
            "generic", "test", 1, 0, 0, 0))
        self.assertTrue(backend.requests)
        self.assertTrue(all("Selected historical context" not in instructions
                            for instructions, _ in backend.requests))
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
