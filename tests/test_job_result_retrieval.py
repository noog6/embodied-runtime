import json
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from embodied_runtime.app import (
    INSPECT_JOB_RESULT_TOOL, ApplicationOptions, RobotApplication,
)
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import JobRunStatus, SQLiteJobStore
from embodied_runtime.profile import RobotProfile
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


class ScriptedBackend(TextCognitionBackend):
    identifier = "job-result-test"

    def __init__(self, calls=(), final="The previous JobRun reported its findings."):
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


class JobResultRetrievalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.store = SQLiteJobStore(self.path)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.temp.cleanup()

    def app(self, backend=None, *, store=True, initiative=False):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=initiative),
            platform_provider=Platform(), cognition_backend=backend or ScriptedBackend(),
            job_store=self.store if store else None,
            wall_clock=lambda: datetime(2026, 9, 26, 12, tzinfo=UTC),
        )

    def terminal(self, job_id, status, summary=None, report=None):
        run = self.store.create_run(job_id)
        if status is not JobRunStatus.STOPPED:
            self.store.transition_run(run.id, JobRunStatus.RUNNING)
        return self.store.transition_run(
            run.id, status,
            outcome_summary=summary if status is JobRunStatus.COMPLETED else None,
            error_summary=summary if status is JobRunStatus.FAILED else None,
            result_report=report,
        )

    async def inspect(self, selector):
        app = self.app()
        await app.start()
        result = app._execute_job_result_inspection(CognitionToolCall(
            INSPECT_JOB_RESULT_TOOL.name, json.dumps({"selector": selector})
        ))
        return json.loads(result.output)

    async def test_projection_is_operator_only_and_separate_from_diagnostics(self):
        app = self.app(initiative=True)
        await app.start()
        self.assertIn("inspect_job_result", [tool.name for tool in app.cognition_tools()])
        app.set_goal("Exercise autonomous projections")
        self.assertNotIn("inspect_job_result", [tool.name for tool in app.acquisition_tools()])
        self.assertNotIn("inspect_job_result", [tool.name for tool in
                                                app._initiative_tools_for_episode(job_work=True)])
        self.assertIn("inspect_job_result", app._acquisition_tool_names())

        disabled = self.app(store=False)
        await disabled.start()
        self.assertNotIn("inspect_job_result", [tool.name for tool in disabled.cognition_tools()])

    async def test_exact_terminal_runs_and_pre_feature_null_report(self):
        job = self.store.create_job("Review logs")
        completed = self.terminal(job.id, JobRunStatus.COMPLETED, "all reviewed",
                                  "First finding. Second finding.")
        failed = self.terminal(job.id, JobRunStatus.FAILED, "provider failed", "Trace review.")
        stopped = self.terminal(job.id, JobRunStatus.STOPPED, report="Stopped by operator.")
        old = self.terminal(job.id, JobRunStatus.COMPLETED, "summary only")

        completed_result = await self.inspect(f"RUN{completed.id}")
        self.assertEqual((completed_result["summary"], completed_result["report"]),
                         ("all reviewed", "First finding. Second finding."))
        self.assertEqual(completed_result["record_authority"], "runtime")
        self.assertEqual(completed_result["content_provenance"], "cognition_work_product")
        self.assertEqual(completed_result["content_authority"],
                         "historical_non_authoritative")
        self.assertEqual((await self.inspect(f"RUN{failed.id}"))["summary"], "provider failed")
        self.assertEqual((await self.inspect(f"RUN{stopped.id}"))["job_run"]["status"],
                         "stopped")
        self.assertIsNone((await self.inspect(f"RUN{old.id}"))["report"])

    async def test_retrieved_at_uses_validated_aware_wall_clock(self):
        job = self.store.create_job("Review logs")
        completed = self.terminal(job.id, JobRunStatus.COMPLETED, "done")
        app = self.app()
        await app.start()
        validated_time = datetime(2026, 9, 26, 16, 30, tzinfo=UTC)
        app._aware_wall_clock = Mock(return_value=validated_time)

        result = app._execute_job_result_inspection(CognitionToolCall(
            INSPECT_JOB_RESULT_TOOL.name,
            json.dumps({"selector": f"RUN{completed.id}"}),
        ))

        app._aware_wall_clock.assert_called_once_with()
        self.assertEqual(json.loads(result.output)["retrieved_at"],
                         validated_time.isoformat())

    async def test_exact_run_missing_running_and_invalid_states(self):
        job = self.store.create_job("Review")
        running = self.store.create_run(job.id)
        self.store.transition_run(running.id, JobRunStatus.RUNNING)
        self.assertEqual((await self.inspect(f"RUN{running.id}"))["reason"],
                         "job_run_not_terminal")
        self.assertEqual((await self.inspect("RUN999"))["reason"], "job_run_not_found")
        self.assertEqual((await self.inspect("run1"))["reason"], "job_not_found")
        self.assertEqual((await self.inspect("JOB01"))["reason"], "invalid_selector")

    async def test_job_lookup_ignores_newer_non_completed_occurrences(self):
        job = self.store.create_job("Review")
        completed = self.terminal(job.id, JobRunStatus.COMPLETED, "retained", "report")
        running = self.store.create_run(job.id)
        self.store.transition_run(running.id, JobRunStatus.RUNNING)
        self.terminal(job.id, JobRunStatus.FAILED, "failed later")
        self.terminal(job.id, JobRunStatus.STOPPED)
        result = await self.inspect(f"JOB{job.id}")
        self.assertEqual(result["job_run"]["id"], completed.id)

        empty = self.store.create_job("Empty")
        self.assertEqual((await self.inspect(f"JOB{empty.id}"))["reason"],
                         "job_has_no_completed_run")
        self.assertEqual((await self.inspect("JOB999"))["reason"], "job_not_found")

    async def test_exact_name_is_trimmed_case_sensitive_and_ambiguity_is_bounded(self):
        job = self.store.create_job("Nightly Self Log Reviewer")
        completed = self.terminal(job.id, JobRunStatus.COMPLETED, "found none")
        result = await self.inspect("  Nightly Self Log Reviewer  ")
        self.assertEqual(result["job_run"]["id"], completed.id)
        self.assertEqual((await self.inspect("nightly self log reviewer"))["reason"],
                         "job_not_found")
        self.assertEqual((await self.inspect("Nightly Self"))["reason"], "job_not_found")
        self.store.create_job("Nightly Self Log Reviewer")
        ambiguous = await self.inspect("Nightly Self Log Reviewer")
        self.assertEqual(ambiguous["reason"], "ambiguous_job_name")
        self.assertEqual(len(ambiguous["candidates"]), 2)

    async def test_operator_budget_and_identical_reuse(self):
        job = self.store.create_job("Review")
        run = self.terminal(job.id, JobRunStatus.COMPLETED, "done")
        backend = ScriptedBackend([
            ("inspect_job_result", {"selector": f"RUN{run.id}"}),
            ("inspect_self", {"area": "runtime"}),
            ("inspect_job_result", {"selector": f"RUN{run.id}"}),
        ])
        app = self.app(backend)
        await app.start()
        await app.request_cognition("What did it find?")
        self.assertNotIn("inspect_job_result", backend.requests[2][1])
        self.assertEqual(backend.results[-1]["status"], "rejected")

        repeated = ScriptedBackend([
            ("inspect_job_result", {"selector": f"RUN{run.id}"}),
            ("inspect_job_result", {"selector": f"RUN{run.id}"}),
        ])
        app2 = self.app(repeated)
        await app2.start()
        await app2.request_cognition("Repeat it")
        self.assertEqual(len(repeated.requests), 3)
        self.assertEqual(repeated.results[0], repeated.results[1])
        self.assertIn("acquisitions_used: 1", repeated.requests[2][0])
        self.assertIn("acquisitions_remaining: 1", repeated.requests[2][0])

    async def test_restart_operator_episode_reads_reopened_durable_result(self):
        job = self.store.create_job("Nightly Self Log Reviewer")
        run = self.terminal(
            job.id, JobRunStatus.COMPLETED, "No application failures were found.",
            "The prior run inspected its bounded logs. It reported no application failures.",
        )
        self.store.close()
        self.store = SQLiteJobStore(self.path)
        backend = ScriptedBackend([
            ("inspect_job_result", {"selector": "Nightly Self Log Reviewer"}),
        ])
        app = self.app(backend)
        await app.start()
        answer = await app.request_cognition("What did it find last night?")
        self.assertEqual(backend.results[0]["job_run"]["id"], run.id)
        self.assertIn("previous JobRun", answer)
        instructions = backend.requests[1][0]
        self.assertIn("runtime-owned durable metadata", instructions)
        self.assertIn("not fresh current runtime evidence", instructions)
        self.assertIn(
            "persistent-memory evidence supports that memory claim", instructions
        )
        self.assertIn(
            "current conditions unless independent fresh evidence establishes the "
            "current claim", instructions
        )
