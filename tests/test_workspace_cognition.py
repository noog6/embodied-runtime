import json
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from embodied_runtime.app import (
    ApplicationOptions, DIAGNOSTIC_TOOLS, MAX_WORKSPACE_COGNITION_WRITE_CHARS,
    RobotApplication, WORKSPACE_LIST_TOOL, WORKSPACE_READ_TOOL, WORKSPACE_WRITE_TOOL,
)
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    FilesystemJobWorkspaceStore, SQLiteJobStore, WorkspaceBackendError,
    WorkspaceConflictError, WorkspaceDurabilityError, WorkspaceNotFoundError,
    WorkspaceQuotaError, WorkspaceUnsafeError, WorkspaceValidationError,
)
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

    async def execute_write(self, arguments):
        app = self.app()
        await app.start()
        return json.loads(app._execute_workspace_write(CognitionToolCall(
            WORKSPACE_WRITE_TOOL.name,
            json.dumps(arguments, ensure_ascii=False))).output)

    async def test_projection_is_operator_only_and_never_an_effect(self):
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
        self.assertTrue({"workspace_list", "workspace_read"}.isdisjoint(
            tool.name for tool in app._initiative_tools_for_episode(job_work=True)))
        self.assertTrue({"workspace_list", "workspace_read"}.isdisjoint(
            tool.name for tool in app.effect_tools()))
        self.assertNotIn("workspace_write", {tool.name for tool in app.acquisition_tools()})
        self.assertNotIn("workspace_write", {tool.name for tool in app.effect_tools()})
        self.assertNotIn("workspace_write", {
            tool.name for tool in app._initiative_tools_for_episode(job_work=True)})
        self.assertNotIn("workspace_write", {
            tool.name for tool in app._continuation_tools_for_episode(
                "schedule_followup", job_work=True)})

        for unavailable in (self.app(jobs=False), self.app(workspaces=False)):
            await unavailable.start()
            self.assertTrue({"workspace_list", "workspace_read"}.isdisjoint(
                tool.name for tool in unavailable.cognition_tools()))
            self.assertNotIn("workspace_write", {
                tool.name for tool in unavailable.cognition_tools()})

    async def test_write_schema_is_strict(self):
        schema = WORKSPACE_WRITE_TOOL.parameters
        self.assertEqual(schema["required"], ["job", "path", "mode", "content"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["mode"]["enum"],
                         ["create", "replace", "append"])
        self.assertEqual(schema["properties"]["content"]["maxLength"],
                         MAX_WORKSPACE_COGNITION_WRITE_CHARS)
        job = self.jobs.create_job("Schema")
        base = {"job": job.name, "path": "note.txt", "mode": "create", "content": "x"}
        invalid = []
        for key in base:
            value = dict(base); value.pop(key); invalid.append(value)
        invalid.extend((dict(base, extra=True), dict(base, mode="upsert"),
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
