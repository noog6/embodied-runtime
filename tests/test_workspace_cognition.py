import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from embodied_runtime.app import (
    ApplicationOptions, DIAGNOSTIC_TOOLS, JOB_OUTCOME_EVALUATION_REQUEST,
    MAX_WORKSPACE_COGNITION_WRITE_CHARS, REPORT_JOB_OUTCOME_TOOL,
    JOB_WORKSPACE_LIST_TOOL, JOB_WORKSPACE_READ_TOOL, JOB_WORKSPACE_WRITE_TOOL,
    RobotApplication, WORKSPACE_LIST_TOOL, WORKSPACE_READ_TOOL, WORKSPACE_WRITE_TOOL,
)
from embodied_runtime.attention import InitiativeOutcome
from embodied_runtime.cognition import (
    CognitionToolCall, InitiativeAcquisitionOutcome, InitiativeEffectOutcome,
    TextCognitionBackend,
)
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    FilesystemJobWorkspaceStore, SQLiteJobStore, WorkspaceBackendError,
    WorkspaceConflictError, WorkspaceDurabilityError, WorkspaceNotFoundError,
    WorkspaceQuotaError, WorkspaceUnsafeError, WorkspaceValidationError,
    JobProgressCounter, JobRunStatus,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.tasks import TaskStatus
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


class ScriptedBackend(TextCognitionBackend):
    identifier = "workspace-cognition-test"

    def __init__(self, calls=(), final="I inspected the Job Workspace."):
        self.calls = list(calls)
        self.final = final
        self.requests = []
        self.results = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((instructions, tuple(tool.name for tool in tools)))
        if self.calls:
            name, arguments = self.calls.pop(0)
            result = await tool_executor(CognitionToolCall(name, json.dumps(arguments)))
            self.results.append(json.loads(result.output))
            return "acquiring"
        return self.final


class AutomaticWorkspaceBackend(TextCognitionBackend):
    identifier = "automatic-workspace-test"

    def __init__(self):
        self.work_requests = 0
        self.outcomes = 0
        self.write_result = None
        self.schemas = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.schemas.append(tuple(tools))
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            self.outcomes += 1
            terminal = self.outcomes == 2
            await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name, json.dumps({
                    "disposition": "completed" if terminal else "continue",
                    "summary": "automatic report written" if terminal else "continue",
                    "report": "immutable run report" if terminal else None,
                    "readiness": None if terminal else "ready",
                    "delay_seconds": None,
                })))
            return "outcome"
        self.work_requests += 1
        if self.work_requests == 2:
            self.write_result = json.loads((await tool_executor(CognitionToolCall(
                "workspace_write", json.dumps({
                    "path": "reports/automatic.md", "mode": "create",
                    "content": "automatic artifact",
                })))).output)
        return "work"


class ConcurrentWorkspaceBackend(TextCognitionBackend):
    """Exercise Workspace tools while exact Job cognition tasks are suspended."""

    identifier = "concurrent-workspace-test"

    def __init__(self, contents):
        self.contents = contents
        self.calls = {source: 0 for source in contents}
        self.read = {source: asyncio.Event() for source in contents}
        self.release = {source: asyncio.Event() for source in contents}
        self.results = {source: [] for source in contents}
        self.after_write = None

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        text = instructions or ""
        source = next((item for item in self.contents if f"source: {item}" in text), None)
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name, json.dumps({
                    "disposition": "completed", "summary": "workspace isolated",
                    "report": None, "readiness": None, "delay_seconds": None,
                }),
            ))
            return "outcome"
        if source is None:
            return "ok"
        call = self.calls[source]
        self.calls[source] += 1
        if call == 0:
            result = await tool_executor(CognitionToolCall(
                "workspace_read", json.dumps({"path": "state.txt", "offset_chars": 0})))
            self.results[source].append(json.loads(result.output))
            self.read[source].set()
            await self.release[source].wait()
            return "read"
        if call == 1:
            result = await tool_executor(CognitionToolCall(
                "workspace_write", json.dumps({
                    "path": "state.txt", "mode": "replace",
                    "content": self.contents[source],
                })))
            self.results[source].append(json.loads(result.output))
            if self.after_write is not None:
                self.after_write(source)
            return "written"
        return "done"


class HeartbeatWorkspaceBackend(TextCognitionBackend):
    identifier = "heartbeat-workspace-test"

    def __init__(self, contents):
        self.contents = contents
        self.work_calls = {source: 0 for source in contents}
        self.outcomes = {source: 0 for source in contents}
        self.results = {source: [] for source in contents}

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        text = instructions or ""
        source = next(item for item in self.contents if f"source: {item}" in text)
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            count = self.outcomes[source]
            self.outcomes[source] += 1
            await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name, json.dumps({
                    "disposition": "continue" if count == 0 else "completed",
                    "summary": "continue" if count == 0 else "done",
                    "report": None, "readiness": "ready" if count == 0 else None,
                    "delay_seconds": None,
                }),
            ))
            return "outcome"
        count = self.work_calls[source]
        self.work_calls[source] += 1
        if count == 1:
            result = await tool_executor(CognitionToolCall(
                "workspace_read", json.dumps({"path": "state.txt", "offset_chars": 0})))
            self.results[source].append(json.loads(result.output))
            return "read"
        if count == 2:
            result = await tool_executor(CognitionToolCall(
                "workspace_write", json.dumps({"path": "state.txt", "mode": "replace",
                                                "content": self.contents[source]})))
            self.results[source].append(json.loads(result.output))
            return "written"
        return "initial"


class ScheduledProgressBackend(TextCognitionBackend):
    """Script the live two-episode report case, optionally retrying the create."""

    identifier = "scheduled-progress-test"

    def __init__(self, *, duplicate=False):
        self.duplicate = duplicate
        self.work_requests = 0
        self.outcomes = 0
        self.requests = []
        self.write_results = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tuple(tool.name for tool in tools)))
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            self.outcomes += 1
            terminal = self.outcomes == 2
            progress = None if terminal else {
                "counter": "report_artifact_written", "basis": "effect_1",
            }
            await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name, json.dumps({
                    "disposition": "completed" if terminal else "continue",
                    "summary": "scheduled report completed" if terminal else "report written",
                    "report": "durable scheduled result" if terminal else None,
                    "readiness": None if terminal else "ready",
                    "delay_seconds": None,
                    "event_type": None,
                    "progress_update": progress,
                }),
            ))
            return "outcome"
        self.work_requests += 1
        if self.work_requests == 1 or self.duplicate:
            result = await tool_executor(CognitionToolCall(
                "workspace_write", json.dumps({
                    "path": "reports/scheduled-test.md", "mode": "create",
                    "content": "scheduled seed",
                }),
            ))
            self.write_results.append(json.loads(result.output))
        return "bounded work"


class WorkspaceCognitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "jobs.sqlite3"
        self.root = Path(self.temp.name) / "workspaces"
        self.jobs = SQLiteJobStore(self.database)
        self.workspaces = FilesystemJobWorkspaceStore(self.root)

    def tearDown(self):
        for store in (self.jobs, self.workspaces):
            try:
                store.close()
            except Exception:
                pass
        self.temp.cleanup()

    def app(self, backend=None, *, jobs=True, workspaces=True, initiative=False,
            capacity=1):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=initiative,
                               jobs_max_concurrent_work=capacity),
            platform_provider=Platform(), cognition_backend=backend or ScriptedBackend(),
            job_store=self.jobs if jobs else None,
            job_workspace_store=self.workspaces if workspaces else None,
            wall_clock=lambda: datetime(2026, 9, 26, 12, tzinfo=UTC),
        )

    async def execute(self, tool, arguments):
        app = self.app()
        await app.start()
        return json.loads(app._execute_workspace_acquisition(CognitionToolCall(
            tool.name, json.dumps(arguments, ensure_ascii=False))).output)

    async def execute_write(self, arguments):
        app = self.app()
        await app.start()
        return json.loads(app._execute_workspace_write(CognitionToolCall(
            WORKSPACE_WRITE_TOOL.name,
            json.dumps(arguments, ensure_ascii=False))).output)

    async def test_projection_separates_operator_autonomy_and_job_work(self):
        app = self.app(initiative=True)
        await app.start()
        operator = {tool.name for tool in app.cognition_tools()}
        self.assertTrue({"workspace_list", "workspace_read"} <= operator)
        self.assertIn("workspace_write", operator)
        self.assertNotIn("workspace_write", {tool.name for tool in DIAGNOSTIC_TOOLS})
        self.assertNotIn("workspace_write", app._acquisition_tool_names())
        app.set_goal("Check autonomous projection")
        self.assertTrue({"workspace_list", "workspace_read"}.isdisjoint(
            tool.name for tool in app.acquisition_tools()))
        self.assertTrue({"workspace_list", "workspace_read", "workspace_write"}.isdisjoint(
            tool.name for tool in app._initiative_tools_for_episode(job_work=True)))
        app.resolve_goal("completed")
        job = self.jobs.create_job("Projection Job")
        app.start_job_run(job.id)
        job_tools = app._initiative_tools_for_episode(job_work=True)
        self.assertTrue({"workspace_list", "workspace_read", "workspace_write"} <= {
            tool.name for tool in job_tools})
        self.assertEqual(len(job_tools), len({tool.name for tool in job_tools}))
        self.assertTrue({"workspace_list", "workspace_read"}.isdisjoint(
            tool.name for tool in app.effect_tools()))
        self.assertNotIn("workspace_write", {tool.name for tool in app.acquisition_tools()})
        self.assertNotIn("workspace_write", {tool.name for tool in app.effect_tools()})
        self.assertIn("workspace_write", {
            tool.name for tool in app._effect_tools_for_episode(job_work=True)})

        for unavailable in (self.app(jobs=False), self.app(workspaces=False)):
            await unavailable.start()
            self.assertTrue({"workspace_list", "workspace_read"}.isdisjoint(
                tool.name for tool in unavailable.cognition_tools()))
            self.assertNotIn("workspace_write", {
                tool.name for tool in unavailable.cognition_tools()})
            unavailable.set_goal("projection")
            self.assertTrue({"workspace_list", "workspace_read", "workspace_write"}.isdisjoint(
                tool.name for tool in unavailable._initiative_tools_for_episode(
                    job_work=True)))

    async def test_job_workspace_schemas_have_no_owner_selector(self):
        for tool in (JOB_WORKSPACE_LIST_TOOL, JOB_WORKSPACE_READ_TOOL,
                     JOB_WORKSPACE_WRITE_TOOL):
            self.assertNotIn("job", tool.parameters["properties"])
            self.assertNotIn("job_id", tool.parameters["properties"])
            self.assertFalse(tool.parameters["additionalProperties"])
        self.assertEqual(JOB_WORKSPACE_LIST_TOOL.parameters["required"],
                         ["directory", "cursor"])
        self.assertEqual(JOB_WORKSPACE_READ_TOOL.parameters["required"],
                         ["path", "offset_chars"])
        self.assertEqual(JOB_WORKSPACE_WRITE_TOOL.parameters["required"],
                         ["path", "mode", "content"])
        self.assertEqual(JOB_WORKSPACE_WRITE_TOOL.parameters["properties"]["mode"]["enum"],
                         ["create", "replace", "append", "upsert"])

    async def test_job_work_upsert_initializes_and_replaces_without_acquisition(self):
        job = self.jobs.create_job("Baseline Steward")
        app = self.app(initiative=True)
        await app.start()
        binding = app.start_job_run(job.id)
        authority = (binding, app._current_task_binding, app.active_goal)

        versions = []
        for content in ("first baseline", "current baseline"):
            result = json.loads(app._execute_job_workspace_write(CognitionToolCall(
                "workspace_write", json.dumps({
                    "path": "baselines/current.txt", "mode": "upsert",
                    "content": content,
                })), job_binding=authority,
                expected_goal=authority[2]).output)
            self.assertEqual(result["status"], "applied")
            self.assertTrue(result["published"])
            self.assertTrue(result["durability_confirmed"])
            self.assertEqual(result["artifact"]["mode"], "upsert")
            versions.append(result["artifact"]["content_version"])

        self.assertNotEqual(*versions)
        self.assertEqual(self.workspaces.read(
            job.id, "baselines/current.txt").content, "current baseline")

    async def test_job_work_uses_own_workspace_and_two_acquisitions_then_write(self):
        job_a = self.jobs.create_job("Nightly Self Log Reviewer")
        job_b = self.jobs.create_job("Other Job")
        self.workspaces.write(job_a.id, "notes/a.md", "create", "A context")
        self.workspaces.write(job_b.id, "notes/b.md", "create", "B secret")
        backend = ScriptedBackend([
            ("workspace_list", {"directory": "", "cursor": None}),
            ("workspace_read", {"path": "notes/a.md", "offset_chars": 0}),
            ("workspace_write", {"path": "reports/current-review.md",
                                 "mode": "create", "content": "review"}),
        ])
        app = self.app(backend, initiative=True)
        await app.start()
        binding = app.start_job_run(job_a.id)

        await app.work_current_job_once()

        self.assertEqual(len(backend.results), 3)
        self.assertTrue(all(result["job"] == {"id": job_a.id, "name": job_a.name}
                            for result in backend.results))
        self.assertEqual(backend.results[-1]["status"], "applied")
        self.assertTrue(backend.results[-1]["published"])
        self.assertTrue(backend.results[-1]["durability_confirmed"])
        self.assertEqual(self.workspaces.read(
            job_a.id, "reports/current-review.md").content, "review")
        with self.assertRaises(WorkspaceNotFoundError):
            self.workspaces.read(job_b.id, "reports/current-review.md")
        self.assertEqual(len(backend.requests), 4)  # initial, two bounded follow-ups, outcome
        outcome_instructions = backend.requests[-1][0]
        self.assertIn("non-authoritative Job Workspace context", outcome_instructions)
        self.assertIn("not evidence that its claims are true or current", outcome_instructions)
        self.assertIn("mutation occurred", outcome_instructions)
        self.assertIs(app.current_job_run, binding)

    async def test_job_workspace_rejects_cross_job_arguments_and_stale_binding(self):
        job_a = self.jobs.create_job("A")
        job_b = self.jobs.create_job("B")
        self.workspaces.write(job_b.id, "notes/b.md", "create", "unchanged")
        app = self.app(initiative=True); await app.start()
        binding = app.start_job_run(job_a.id)
        captured = (binding, app._current_task_binding, app.active_goal)
        read = app._execute_job_workspace_acquisition(CognitionToolCall(
            "workspace_read", json.dumps({"job": "B", "path": "notes/b.md",
                                           "offset_chars": 0})),
            job_binding=captured, expected_goal=app.active_goal)
        write = app._execute_job_workspace_write(CognitionToolCall(
            "workspace_write", json.dumps({"job_id": job_b.id, "path": "notes/b.md",
                                            "mode": "replace", "content": "changed"})),
            job_binding=captured, expected_goal=app.active_goal)
        self.assertEqual(json.loads(read.output)["reason"], "invalid_tool_arguments")
        self.assertEqual(json.loads(write.output)["reason"], "invalid_tool_arguments")
        self.assertEqual(self.workspaces.read(job_b.id, "notes/b.md").content,
                         "unchanged")
        app.pause_task()
        stale = app._execute_job_workspace_write(CognitionToolCall(
            "workspace_write", json.dumps({"path": "x.md", "mode": "create",
                                            "content": "x"})),
            job_binding=captured, expected_goal=captured[2])
        self.assertEqual(json.loads(stale.output)["reason"], "stale_job_work_binding")

    async def test_inflight_job_workspace_authority_survives_foreground_change(self):
        jobs = [self.jobs.create_job(name) for name in ("A", "B")]
        for job in jobs:
            self.workspaces.write(job.id, "state.txt", "create", job.name)
        source_a = None
        backend = ConcurrentWorkspaceBackend({})
        app = self.app(backend, initiative=True, capacity=2)
        await app.start()
        binding_a = app.start_job_run(jobs[0].id)
        source_a = f"JOB{binding_a.job.id}/RUN{binding_a.run.id}"
        backend.contents[source_a] = "A updated"
        backend.calls[source_a] = 0
        backend.read[source_a] = asyncio.Event()
        backend.release[source_a] = asyncio.Event()
        backend.results[source_a] = []
        context_a = app._context_for_run(binding_a.run.id)
        identity = (context_a, context_a.binding, context_a.task_binding,
                    context_a.task_binding.active_goal)
        identity_after_write = []
        backend.after_write = lambda _: identity_after_write.append((
            context_a, context_a.binding, context_a.task_binding,
            context_a.task_binding.active_goal,
        ))

        work_a = asyncio.create_task(app.work_job_run_once(binding_a.run.id))
        await backend.read[source_a].wait()
        binding_b = app.start_job_run(jobs[1].id)
        self.assertEqual(app.current_job_execution_context.run_id, binding_b.run.id)
        backend.release[source_a].set()
        outcome = await work_a

        self.assertEqual((outcome.job_id, outcome.run_id),
                         (binding_a.job.id, binding_a.run.id))
        self.assertEqual([result["status"] for result in backend.results[source_a]],
                         ["ok", "applied"])
        self.assertEqual(self.workspaces.read(jobs[0].id, "state.txt").content,
                         "A updated")
        self.assertEqual(self.workspaces.read(jobs[1].id, "state.txt").content, "B")
        self.assertEqual(identity_after_write, [identity])

    async def test_simultaneous_job_workspaces_remain_isolated_from_foreground(self):
        jobs = [self.jobs.create_job(name) for name in ("A", "B")]
        for job in jobs:
            self.workspaces.write(job.id, "state.txt", "create", job.name)
        app = self.app(initiative=True, capacity=2)
        await app.start()
        bindings = [app.start_job_run(job.id) for job in jobs]
        sources = [f"JOB{item.job.id}/RUN{item.run.id}" for item in bindings]
        backend = ConcurrentWorkspaceBackend(dict(zip(sources, ("A updated", "B updated"))))
        app._cognition_backend = backend
        tasks = [asyncio.create_task(app.work_job_run_once(item.run.id))
                 for item in bindings]
        await asyncio.gather(*(backend.read[source].wait() for source in sources))
        app._foreground_job_run_id = bindings[0].run.id
        for event in backend.release.values():
            event.set()
        outcomes = await asyncio.gather(*tasks)

        self.assertEqual({(item.job_id, item.run_id) for item in outcomes},
                         {(item.job.id, item.run.id) for item in bindings})
        for job, source, expected in zip(jobs, sources, ("A updated", "B updated")):
            self.assertEqual([result["status"] for result in backend.results[source]],
                             ["ok", "applied"])
            self.assertEqual(backend.results[source][0]["artifact"]["content"], job.name)
            self.assertEqual(self.workspaces.read(job.id, "state.txt").content, expected)

    async def test_heartbeat_continuations_keep_exact_workspace_authority(self):
        jobs = [self.jobs.create_job(name) for name in ("A", "B")]
        for job in jobs:
            self.workspaces.write(job.id, "state.txt", "create", job.name)
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True, jobs_auto_continue=True,
                               jobs_max_concurrent_work=2),
            platform_provider=Platform(), cognition_backend=ScriptedBackend(),
            job_store=self.jobs, job_workspace_store=self.workspaces,
            wall_clock=lambda: datetime(2026, 9, 26, 12, tzinfo=UTC),
            job_continuation_sleep=lambda _: asyncio.Event().wait(),
        )
        await app.start()
        bindings = [app.start_job_run(job.id) for job in jobs]
        sources = [f"JOB{item.job.id}/RUN{item.run.id}" for item in bindings]
        backend = HeartbeatWorkspaceBackend(dict(zip(sources, ("A heartbeat", "B heartbeat"))))
        app._cognition_backend = backend
        await asyncio.gather(*(app.work_job_run_once(item.run.id) for item in bindings))
        contexts = [app._context_for_run(item.run.id) for item in bindings]
        self.assertTrue(all(context.continuation is not None for context in contexts))

        app._foreground_job_run_id = bindings[1].run.id
        app._offer_job_continuation()
        tasks = [context.active_work_task for context in contexts]
        self.assertTrue(all(task is not None for task in tasks))
        self.assertNotEqual(app.current_job_execution_context.run_id, bindings[0].run.id)
        await asyncio.gather(*tasks)

        for job, source, expected in zip(jobs, sources,
                                         ("A heartbeat", "B heartbeat")):
            self.assertEqual([result["status"] for result in backend.results[source]],
                             ["ok", "applied"])
            self.assertEqual(self.workspaces.read(job.id, "state.txt").content, expected)

    def test_workspace_acquisitions_are_not_progress_bases_and_ordinals_stay_fixed(self):
        initiative = InitiativeOutcome("done", acquisitions=(
            InitiativeAcquisitionOutcome("workspace_read", "applied", "{}"),
            InitiativeAcquisitionOutcome("inspect_run_history", "applied", "{}"),
        ), effects=(InitiativeEffectOutcome("workspace_write", "applied", "{}"),))
        self.assertEqual(RobotApplication._job_progress_bases(initiative, None),
                         ("acquisition_2", "effect_1"))

    async def test_automatic_job_work_writes_and_result_remains_independent_on_restart(self):
        backend = AutomaticWorkspaceBackend()
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True, jobs_auto_continue=True,
                               jobs_max_auto_steps=1),
            platform_provider=Platform(), cognition_backend=backend,
            job_store=self.jobs, job_workspace_store=self.workspaces,
            wall_clock=lambda: datetime(2026, 9, 26, 12, tzinfo=UTC),
        )
        job = self.jobs.create_job("Automatic Reviewer")
        await app.start(); binding = app.start_job_run(job.id)
        await app.work_current_job_once()
        app._offer_job_continuation()
        task = app._active_job_work_task
        self.assertIsNotNone(task)
        await task
        self.assertEqual(backend.write_result["status"], "applied")
        write_schema = next(tool for tools in backend.schemas for tool in tools
                            if tool.name == "workspace_write"
                            and "job" not in tool.parameters["properties"])
        self.assertEqual(set(write_schema.parameters["properties"]),
                         {"path", "mode", "content"})
        self.assertEqual(self.jobs.get_run(binding.run.id).result_report,
                         "immutable run report")
        await app.stop(); self.jobs.close(); self.workspaces.close()
        self.jobs = SQLiteJobStore(self.database)
        self.workspaces = FilesystemJobWorkspaceStore(self.root)
        self.assertEqual(self.workspaces.read(
            job.id, "reports/automatic.md").content, "automatic artifact")
        self.assertEqual(self.jobs.get_run(binding.run.id).result_report,
                         "immutable run report")
        self.workspaces.write(job.id, "reports/automatic.md", "replace", "revised")
        self.assertEqual(self.jobs.get_run(binding.run.id).result_report,
                         "immutable run report")

    async def test_prior_progress_completes_scheduled_report_without_redundant_write(self):
        backend = ScheduledProgressBackend()
        app = self.app(backend, initiative=True)
        job = self.jobs.create_job(
            "Scheduled Workspace Test",
            description="Create one report artifact from the seed.",
        )
        await app.start(); binding = app.start_job_run(job.id)
        with patch.object(self.workspaces, "write",
                          wraps=self.workspaces.write) as write_mock, \
                patch.object(app, "finish_task", wraps=app.finish_task) as finish_mock:
            first = await app.work_current_job_once()
            self.assertEqual(
                app.job_progress.counters,
                (JobProgressCounter("report_artifact_written", 1),),
            )
            second = await app.work_current_job_once()

        second_initial = next(
            instructions for message, instructions, _tools in backend.requests[2:]
            if message != JOB_OUTCOME_EVALUATION_REQUEST
        )
        second_outcome = [instructions for message, instructions, _tools
                          in backend.requests
                          if message == JOB_OUTCOME_EVALUATION_REQUEST][1]
        self.assertIn("Current Job progress", second_initial)
        self.assertIn("report_artifact_written: 1", second_initial)
        self.assertIn("report_artifact_written: 1", second_outcome)
        self.assertIn("already-earned bounded step", JOB_OUTCOME_EVALUATION_REQUEST)
        self.assertEqual(write_mock.call_count, 1)
        self.assertEqual(backend.write_results[0]["status"], "applied")
        self.assertEqual(self.workspaces.read(
            job.id, "reports/scheduled-test.md").content, "scheduled seed")
        self.assertEqual(first.disposition.value, "continue")
        self.assertEqual(second.disposition.value, "completed")
        self.assertIsNone(app.current_job_run)
        self.assertIsNone(app.job_progress)
        self.assertIs(self.jobs.get_run(binding.run.id).status, JobRunStatus.COMPLETED)
        self.assertEqual(self.jobs.get_run(binding.run.id).result_report,
                         "durable scheduled result")
        finish_mock.assert_called_once_with(TaskStatus.COMPLETED)
        self.assertIsNone(app.current_task)
        await app.stop()

    async def test_rejected_duplicate_create_does_not_erase_prior_progress(self):
        backend = ScheduledProgressBackend(duplicate=True)
        app = self.app(backend, initiative=True)
        job = self.jobs.create_job("Scheduled Workspace Test")
        await app.start(); binding = app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertEqual(app.job_progress.counters,
                         (JobProgressCounter("report_artifact_written", 1),))

        outcome = await app.work_current_job_once()

        self.assertEqual([result["status"] for result in backend.write_results],
                         ["applied", "rejected"])
        self.assertEqual(backend.write_results[1]["reason"], "artifact_exists")
        self.assertEqual(outcome.disposition.value, "completed")
        self.assertIs(self.jobs.get_run(binding.run.id).status, JobRunStatus.COMPLETED)
        self.assertIsNone(app.job_progress)
        await app.stop()

    async def test_write_schema_is_strict(self):
        schema = WORKSPACE_WRITE_TOOL.parameters
        self.assertEqual(schema["required"], ["job", "path", "mode", "content"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["mode"]["enum"],
                         ["create", "replace", "append", "upsert"])
        self.assertEqual(schema["properties"]["content"]["maxLength"],
                         MAX_WORKSPACE_COGNITION_WRITE_CHARS)
        job = self.jobs.create_job("Schema")
        base = {"job": job.name, "path": "note.txt", "mode": "create", "content": "x"}
        invalid = []
        for key in base:
            value = dict(base); value.pop(key); invalid.append(value)
        invalid.extend((dict(base, extra=True), dict(base, mode="merge"),
                        dict(base, job=1), dict(base, path=1), dict(base, content=1),
                        dict(base, content="x" * (MAX_WORKSPACE_COGNITION_WRITE_CHARS + 1))))
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.assertEqual((await self.execute_write(arguments))["status"], "rejected")

    async def test_generic_cognition_dispatch_cannot_write(self):
        job = self.jobs.create_job("Workspace")
        app = self.app()
        await app.start()
        result = json.loads((await app._execute_cognition_tool(CognitionToolCall(
            WORKSPACE_WRITE_TOOL.name,
            json.dumps({
                "job": job.name, "path": "notes/generic.md",
                "mode": "create", "content": "must not be written",
            }),
        ))).output)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("tool is not available", result["error"])
        with self.assertRaises(WorkspaceNotFoundError):
            self.workspaces.read(job.id, "notes/generic.md")

    async def test_create_append_replace_and_exact_selector(self):
        job = self.jobs.create_job("Workspace Smoke Test")
        created = await self.execute_write({
            "job": "  Workspace Smoke Test  ", "path": "notes/mira-thoughts.md",
            "mode": "create", "content": "I have a desk now.",
        })
        self.assertEqual(created["status"], "applied")
        self.assertTrue(created["published"] and created["durability_confirmed"])
        self.assertEqual(created["job"], {"id": job.id, "name": job.name})
        self.assertNotIn("content", created["artifact"])
        old_version = created["artifact"]["content_version"]
        appended = await self.execute_write({
            "job": f"JOB{job.id}", "path": "notes/mira-thoughts.md",
            "mode": "append", "content": "\nAnd a pencil.",
        })
        self.assertNotEqual(appended["artifact"]["content_version"], old_version)
        self.assertEqual(self.workspaces.read(job.id, "notes/mira-thoughts.md").content,
                         "I have a desk now.\nAnd a pencil.")
        replaced = await self.execute_write({
            "job": job.name, "path": "notes/mira-thoughts.md",
            "mode": "replace", "content": "Revised thought.",
        })
        self.assertEqual(replaced["artifact"]["size_bytes"], len(b"Revised thought."))
        self.assertEqual(self.workspaces.read(job.id, "notes/mira-thoughts.md").content,
                         "Revised thought.")
        self.assertEqual(self.jobs.list_runs(job.id), ())
        for selector in ("workspace smoke test", "Workspace Smoke", "JOB01", "RUN1"):
            result = await self.execute_write(dict(
                job=selector, path="other.txt", mode="create", content="x"))
            self.assertNotEqual(result["status"], "applied")

    async def test_conflict_missing_quota_and_failures_are_bounded(self):
        job = self.jobs.create_job("Workspace")
        self.workspaces.write(job.id, "existing.md", "create", "original")
        exists = await self.execute_write({
            "job": job.name, "path": "existing.md", "mode": "create",
            "content": "replacement"})
        self.assertEqual((exists["status"], exists["reason"]),
                         ("rejected", "artifact_exists"))
        self.assertEqual(self.workspaces.read(job.id, "existing.md").content, "original")
        for mode in ("replace", "append"):
            missing = await self.execute_write({
                "job": job.name, "path": f"{mode}.md", "mode": mode, "content": "x"})
            self.assertEqual((missing["status"], missing["reason"]),
                             ("not_found", "artifact_not_found"))
        multibyte = await self.execute_write({
            "job": job.name, "path": "large.md", "mode": "create",
            "content": "€" * MAX_WORKSPACE_COGNITION_WRITE_CHARS})
        self.assertEqual(multibyte["reason"], "workspace_quota_exceeded")

        mappings = ((WorkspaceValidationError("host path"), "rejected"),
                    (WorkspaceQuotaError("details"), "rejected"),
                    (WorkspaceConflictError("changed"), "rejected"),
                    (WorkspaceNotFoundError("details"), "not_found"),
                    (WorkspaceUnsafeError("details"), "unsafe"),
                    (WorkspaceBackendError("/secret/root"), "unavailable"),
                    (WorkspaceDurabilityError("/secret/root"), "indeterminate"))
        args = {"job": job.name, "path": "target.md", "mode": "create", "content": "x"}
        for error, status in mappings:
            with self.subTest(error=type(error).__name__), patch.object(
                    self.workspaces, "write", side_effect=error):
                result = await self.execute_write(args)
                self.assertEqual(result["status"], status)
                self.assertNotIn("/secret", json.dumps(result))
                if isinstance(error, WorkspaceDurabilityError):
                    self.assertTrue(result["published"])
                    self.assertFalse(result["durability_confirmed"])

    async def test_read_then_write_and_two_acquisitions_then_write(self):
        job = self.jobs.create_job("Workspace")
        self.workspaces.write(job.id, "note.md", "create", "old")
        backend = ScriptedBackend([
            ("workspace_read", {"job": job.name, "path": "note.md"}),
            ("workspace_write", {"job": job.name, "path": "note.md",
                                 "mode": "append", "content": "+new"}),
            ("workspace_write", {"job": job.name, "path": "second.md",
                                 "mode": "create", "content": "never"}),
        ])
        app = self.app(backend); await app.start()
        await app.request_cognition("Read and append the authorized text")
        self.assertEqual(self.workspaces.read(job.id, "note.md").content, "old+new")
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(len(backend.results), 2)

        backend2 = ScriptedBackend([
            ("workspace_list", {"job": job.name, "directory": ""}),
            ("workspace_read", {"job": job.name, "path": "note.md"}),
            ("workspace_write", {"job": job.name, "path": "note.md",
                                 "mode": "append", "content": "!"}),
        ])
        app2 = self.app(backend2); await app2.start()
        await app2.request_cognition("Inspect twice, then append")
        self.assertNotIn("workspace_read", backend2.requests[2][1])
        self.assertIn("workspace_write", backend2.requests[2][1])
        self.assertEqual(self.workspaces.read(job.id, "note.md").content, "old+new!")

    async def test_operator_grounding_and_restart_after_cognition_write(self):
        job = self.jobs.create_job("Workspace Smoke Test")
        backend = ScriptedBackend([("workspace_write", {
            "job": job.name, "path": "notes/restart.md", "mode": "create",
            "content": "exact\r\ntext",
        })])
        app = self.app(backend); await app.start()
        await app.request_cognition("Write this exact note in the named Workspace")
        instructions = backend.requests[0][0]
        for phrase in ("current operator request explicitly requests or clearly authorizes",
                       "status=applied with published=true and durability_confirmed=true",
                       "claim neither confirmed failure nor confirmed durable success",
                       "non-authoritative authored working material",
                       "Workspace write is not persistent memory"):
            self.assertIn(phrase, instructions)
        version = backend.results[0]["artifact"]["content_version"]
        self.jobs.close(); self.workspaces.close()
        self.jobs = SQLiteJobStore(self.database)
        self.workspaces = FilesystemJobWorkspaceStore(self.root)
        reopened = self.workspaces.read(job.id, "notes/restart.md")
        self.assertEqual((reopened.content, reopened.content_version),
                         ("exact\r\ntext", version))
        self.assertEqual(self.jobs.list_runs(job.id), ())

    async def test_exact_selectors_empty_listing_and_ambiguity(self):
        job = self.jobs.create_job("Workspace Smoke Test")
        empty = await self.execute(WORKSPACE_LIST_TOOL, {"job": f"JOB{job.id}"})
        self.assertEqual((empty["status"], empty["entries"]), ("ok", []))
        self.assertEqual(empty["record_authority"], "runtime")
        self.assertEqual(empty["content_authority"],
                         "authored_working_material_non_authoritative")
        self.assertEqual((await self.execute(WORKSPACE_LIST_TOOL, {
            "job": "  Workspace Smoke Test  "}))["job"]["id"], job.id)
        for selector in ("workspace smoke test", "Workspace Smoke", "JOB01", "JOB999"):
            result = await self.execute(WORKSPACE_LIST_TOOL, {"job": selector})
            self.assertNotEqual(result["status"], "ok")
        self.jobs.create_job("Workspace Smoke Test")
        ambiguous = await self.execute(WORKSPACE_LIST_TOOL,
                                       {"job": "Workspace Smoke Test"})
        self.assertEqual(ambiguous["reason"], "ambiguous_job_name")
        self.assertEqual(len(ambiguous["candidates"]), 2)

    async def test_list_and_read_structured_bounds_and_preservation(self):
        job = self.jobs.create_job("Workspace Smoke Test")
        text = "héllo\r\nworld\n" + "x" * 8_010
        written = self.workspaces.write(job.id, "artifacts/report.md", "create", text)
        root = await self.execute(WORKSPACE_LIST_TOOL,
                                  {"job": job.name, "directory": "", "cursor": None})
        self.assertEqual(root["entries"][0]["path"], "artifacts")
        listing = await self.execute(WORKSPACE_LIST_TOOL,
                                     {"job": job.name, "directory": "artifacts"})
        entry = listing["entries"][0]
        self.assertEqual(entry["content_version"], written.content_version)
        self.assertNotIn(str(self.root), json.dumps(listing))

        read = await self.execute(WORKSPACE_READ_TOOL,
                                  {"job": f"JOB{job.id}", "path": "artifacts/report.md"})
        artifact = read["artifact"]
        self.assertEqual(artifact["content"], text[:8_000])
        self.assertTrue(artifact["truncated"])
        self.assertEqual(artifact["content_version"], written.content_version)
        self.assertEqual(read["content_provenance"], "authored_working_material")
        self.assertEqual(read["content_authority"], "non_authoritative")
        tail = await self.execute(WORKSPACE_READ_TOOL, {
            "job": job.name, "path": "artifacts/report.md",
            "offset_chars": artifact["next_offset_chars"],
        })
        self.assertEqual(artifact["content"] + tail["artifact"]["content"], text)
        eof = await self.execute(WORKSPACE_READ_TOOL, {
            "job": job.name, "path": "artifacts/report.md", "offset_chars": len(text),
        })
        self.assertEqual(eof["artifact"]["content"], "")

    async def test_failure_mapping_is_bounded(self):
        job = self.jobs.create_job("Workspace")
        self.workspaces.write(job.id, "present/file.txt", "create", "text")
        cases = (
            (WORKSPACE_LIST_TOOL, {"job": job.name, "directory": "missing"},
             "directory_not_found"),
            (WORKSPACE_LIST_TOOL, {"job": job.name, "cursor": "bad"}, "invalid_cursor"),
            (WORKSPACE_READ_TOOL, {"job": job.name, "path": "missing"},
             "artifact_not_found"),
            (WORKSPACE_READ_TOOL, {"job": job.name, "path": "../bad"},
             "invalid_logical_path"),
            (WORKSPACE_READ_TOOL, {"job": job.name, "path": "missing", "offset_chars": -1},
             "invalid_read_offset"),
        )
        for tool, arguments, reason in cases:
            with self.subTest(reason=reason):
                result = await self.execute(tool, arguments)
                self.assertEqual(result["reason"], reason)
                self.assertNotIn(str(self.root), json.dumps(result))

    async def test_listing_page_cursor_and_malformed_utf8_are_bounded(self):
        job = self.jobs.create_job("Many files")
        for index in range(101):
            self.workspaces.write(job.id, f"pages/{index:03}.txt", "create", "x")
        first = await self.execute(WORKSPACE_LIST_TOOL, {
            "job": job.name, "directory": "pages", "cursor": None,
        })
        self.assertEqual(len(first["entries"]), 100)
        self.assertIsNotNone(first["next_cursor"])
        second = await self.execute(WORKSPACE_LIST_TOOL, {
            "job": job.name, "directory": "pages", "cursor": first["next_cursor"],
        })
        self.assertEqual(len(second["entries"]), 1)
        stale = await self.execute(WORKSPACE_LIST_TOOL, {
            "job": job.name, "directory": "", "cursor": first["next_cursor"],
        })
        self.assertEqual(stale["reason"], "invalid_cursor")

        malformed = self.root / f"JOB{job.id}" / "pages" / "bad.txt"
        malformed.write_bytes(b"\xff")
        unsafe = await self.execute(WORKSPACE_READ_TOOL, {
            "job": job.name, "path": "pages/bad.txt", "offset_chars": 0,
        })
        self.assertEqual(unsafe, {"status": "unsafe", "reason": "unsafe_workspace_entry"})

        beyond = await self.execute(WORKSPACE_READ_TOOL, {
            "job": job.name, "path": "pages/000.txt", "offset_chars": 2,
        })
        self.assertEqual(beyond["reason"], "invalid_read_offset")

    async def test_operator_budget_duplicate_reuse_and_no_job_run_regression(self):
        job = self.jobs.create_job("Workspace Smoke Test")
        self.workspaces.write(job.id, "artifacts/report.md", "create", "durable report")
        backend = ScriptedBackend([
            ("workspace_list", {"job": job.name, "directory": ""}),
            ("workspace_list", {"job": job.name, "directory": "artifacts"}),
            ("workspace_read", {"job": job.name, "path": "artifacts/report.md"}),
        ])
        app = self.app(backend)
        await app.start()
        await app.request_cognition("What files are currently in the Workspace?")
        self.assertEqual(backend.results[0]["entries"][0]["path"], "artifacts")
        self.assertEqual(backend.results[1]["entries"][0]["path"],
                         "artifacts/report.md")
        self.assertNotIn("workspace_read", backend.requests[2][1])
        self.assertEqual(backend.results[2]["status"], "rejected")
        instructions = backend.requests[1][0]
        self.assertIn("may exist when the Job has never run", instructions)
        self.assertIn("JobRun results do not represent Workspace contents", instructions)
        self.assertIn("not persistent memory or fresh runtime/current-world evidence",
                      instructions)

        repeated = ScriptedBackend([
            ("workspace_read", {"job": job.name, "path": "artifacts/report.md"}),
            ("workspace_read", {"job": job.name, "path": "artifacts/report.md"}),
        ])
        app2 = self.app(repeated)
        await app2.start()
        await app2.request_cognition("Read it twice")
        self.assertEqual(repeated.results[0], repeated.results[1])
        self.assertEqual(len(repeated.requests), 3)

    async def test_duplicate_read_reuse_reaches_bounded_workspace_write(self):
        job = self.jobs.create_job("Workspace Smoke Test")
        path = "notes/mira-thoughts.md"
        self.workspaces.write(job.id, path, "create", "Original thought.")
        read = {"job": job.name, "path": path}
        backend = ScriptedBackend([
            ("workspace_read", read),
            ("workspace_read", read),
            ("workspace_write", {
                "job": job.name,
                "path": path,
                "mode": "append",
                "content": "\nRecovered after duplicate reuse.",
            }),
        ])
        app = self.app(backend)
        await app.start()

        with patch.object(self.workspaces, "read", wraps=self.workspaces.read) as read_mock, \
                patch.object(self.workspaces, "write",
                             wraps=self.workspaces.write) as write_mock:
            await app.request_cognition(
                "Read notes/mira-thoughts.md in the Workspace Smoke Test job, "
                "then append one sentence."
            )

        self.assertEqual(read_mock.call_count, 1)
        self.assertEqual(write_mock.call_count, 1)
        self.assertEqual(len(backend.requests), 3)
        self.assertEqual(len(backend.results), 3)
        self.assertEqual(backend.results[0], backend.results[1])
        self.assertIn("acquisitions_used: 1", backend.requests[2][0])
        self.assertIn("acquisitions_remaining: 1", backend.requests[2][0])
        self.assertIn("workspace_read", backend.requests[2][1])
        self.assertIn("workspace_write", backend.requests[2][1])
        self.assertEqual(
            [outcome.name for outcome in app.working_memory.snapshot()[0].tool_outcomes],
            ["workspace_read", "workspace_write"],
        )
        self.assertEqual(
            self.workspaces.read(job.id, path).content,
            "Original thought.\nRecovered after duplicate reuse.",
        )
        self.assertEqual(app.episode_coordinator.last.completion_reason, "handled")

    async def test_duplicate_reuse_cannot_extend_three_stage_grammar(self):
        job = self.jobs.create_job("Workspace Smoke Test")
        path = "notes/mira-thoughts.md"
        self.workspaces.write(job.id, path, "create", "Original thought.")
        read = {"job": job.name, "path": path}
        backend = ScriptedBackend([
            ("workspace_read", read),
            ("workspace_read", read),
            ("workspace_read", read),
            ("workspace_write", {
                "job": job.name, "path": path, "mode": "append",
                "content": "must not be written",
            }),
        ])
        app = self.app(backend)
        await app.start()

        with patch.object(self.workspaces, "read", wraps=self.workspaces.read) as read_mock, \
                patch.object(self.workspaces, "write",
                             wraps=self.workspaces.write) as write_mock:
            await app.request_cognition("Keep reading, then write")

        self.assertEqual(len(backend.requests), 3)
        self.assertEqual(read_mock.call_count, 1)
        self.assertEqual(write_mock.call_count, 0)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(
            [outcome.name for outcome in app.working_memory.snapshot()[0].tool_outcomes],
            ["workspace_read"],
        )

    async def test_restart_reads_reopened_workspace_without_a_run(self):
        job = self.jobs.create_job("Workspace Smoke Test")
        self.workspaces.write(job.id, "artifacts/report.md", "create", "after restart")
        self.jobs.close()
        self.workspaces.close()
        self.jobs = SQLiteJobStore(self.database)
        self.workspaces = FilesystemJobWorkspaceStore(self.root)
        backend = ScriptedBackend([
            ("workspace_read", {"job": job.name, "path": "artifacts/report.md"}),
        ])
        app = self.app(backend)
        await app.start()
        await app.request_cognition("What does the Workspace artifact say?")
        self.assertEqual(backend.results[0]["artifact"]["content"], "after restart")
        self.assertEqual(self.jobs.list_runs(job.id), ())
        self.assertIsNone(app.current_task)


if __name__ == "__main__":
    unittest.main()
