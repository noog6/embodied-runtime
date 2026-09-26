import json
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import (
    ApplicationOptions, RobotApplication, WORKSPACE_LIST_TOOL, WORKSPACE_READ_TOOL,
)
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import FilesystemJobWorkspaceStore, SQLiteJobStore
from embodied_runtime.profile import RobotProfile
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

    def app(self, backend=None, *, jobs=True, workspaces=True, initiative=False):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=initiative),
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

    async def test_projection_is_operator_only_and_never_an_effect(self):
        app = self.app(initiative=True)
        await app.start()
        operator = {tool.name for tool in app.cognition_tools()}
        self.assertTrue({"workspace_list", "workspace_read"} <= operator)
        app.set_goal("Check autonomous projection")
        self.assertTrue({"workspace_list", "workspace_read"}.isdisjoint(
            tool.name for tool in app.acquisition_tools()))
        self.assertTrue({"workspace_list", "workspace_read"}.isdisjoint(
            tool.name for tool in app._initiative_tools_for_episode(job_work=True)))
        self.assertTrue({"workspace_list", "workspace_read"}.isdisjoint(
            tool.name for tool in app.effect_tools()))

        for unavailable in (self.app(jobs=False), self.app(workspaces=False)):
            await unavailable.start()
            self.assertTrue({"workspace_list", "workspace_read"}.isdisjoint(
                tool.name for tool in unavailable.cognition_tools()))

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
        self.assertEqual(len(repeated.requests), 2)

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
