import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import JOB_OUTCOME_EVALUATION_REQUEST, REPORT_JOB_OUTCOME_TOOL
from embodied_runtime.cognition import CognitionToolCall
from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    MAX_JOB_CONTINUITY_SUMMARY_CHARS, JobRunStatus,
    SQLiteJobStore,
)
from embodied_runtime.profile import RobotProfile
from tests.test_job_execution import FakeTimer, JobBackend, Platform


HEADING = "Previous bounded Job work"


class SummaryBackend(JobBackend):
    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = iter(outcomes)

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tuple(tool.name for tool in tools)))
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            disposition, summary = next(self.outcomes)
            await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name,
                json.dumps({"disposition": disposition, "summary": summary}),
            ))
        return "current episode commentary"


class PriorContextCannotTerminalizeBackend(SummaryBackend):
    def __init__(self):
        super().__init__(())
        self.outcome_number = 0

    async def respond(self, message, **kwargs):
        instructions = kwargs.get("instructions") or ""
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            # Simulate a backend that would make the unsafe proposal if continuity
            # leaked into the authoritative outcome-evidence request.
            self.outcome_number += 1
            leaked = "Condition X is currently satisfied" in instructions
            disposition = "completed" if leaked else "continue"
            summary = (
                "Condition X is currently satisfied."
                if self.outcome_number == 1 else "checked"
            )
            self.requests.append((message, instructions,
                                  tuple(tool.name for tool in kwargs.get("tools", ()))))
            await kwargs["tool_executor"](CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name,
                json.dumps({"disposition": disposition, "summary": summary}),
            ))
            return "evaluation"
        return await super().respond(message, **kwargs)


class JobSemanticContinuityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def app(self, backend, *, timer=None):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(
                initiative_enabled=True, initiative_goal_closure_enabled=True,
                jobs_auto_continue=True, jobs_heartbeat_seconds=30,
                jobs_max_auto_steps=3,
            ),
            platform_provider=Platform(), cognition_backend=backend,
            job_store=self.store,
            wall_clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
            job_continuation_sleep=None if timer is None else timer.sleep,
        )

    async def _tick(self, timer):
        await timer.advance()
        for _ in range(12):
            await asyncio.sleep(0)

    async def test_first_episode_has_no_continuity_section(self):
        backend = SummaryBackend((("continue", "summary A"),))
        app = self.app(backend)
        await app.start()
        job = self.store.create_job("Inspect", "Durable description")
        app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertNotIn(HEADING, backend.requests[0][1])
        self.assertIn("Durable description", backend.requests[0][1])
        self.assertIn("Complete JOB1: Inspect", backend.requests[0][1])
        await app.stop()

    async def test_automatic_episode_receives_exactly_one_latest_summary(self):
        timer = FakeTimer()
        backend = SummaryBackend((
            ("continue", "summary A"), ("continue", "summary B"),
            ("continue", "summary C"),
        ))
        app = self.app(backend, timer=timer)
        await app.start()
        app.start_job_run(self.store.create_job("Repeat").id)
        await app.work_current_job_once()
        await self._tick(timer)
        second = backend.requests[2][1]
        self.assertEqual(second.count(HEADING), 1)
        self.assertIn("summary A", second)
        await self._tick(timer)
        third = backend.requests[4][1]
        self.assertEqual(third.count(HEADING), 1)
        self.assertIn("summary B", third)
        self.assertNotIn("summary A", third)
        await app.stop()

    async def test_manual_work_uses_and_replaces_armed_summary(self):
        backend = SummaryBackend((
            ("continue", "summary A"), ("continue", "summary B"),
        ))
        app = self.app(backend)
        await app.start()
        app.start_job_run(self.store.create_job("Manual").id)
        await app.work_current_job_once()
        await app.work_current_job_once()
        self.assertIn("summary A", backend.requests[2][1])
        self.assertEqual(app.job_continuation.last_summary, "summary B")
        await app.stop()

    async def test_projection_truncates_without_mutating_source_summary(self):
        long_summary = "🟣" * (MAX_JOB_CONTINUITY_SUMMARY_CHARS + 20)
        backend = SummaryBackend((
            ("continue", long_summary), ("continue", "next"),
        ))
        app = self.app(backend)
        await app.start()
        app.start_job_run(self.store.create_job("Unicode").id)
        await app.work_current_job_once()
        self.assertEqual(app.job_continuation.last_summary, long_summary)
        await app.work_current_job_once()
        instructions = backend.requests[2][1]
        self.assertIn("🟣" * MAX_JOB_CONTINUITY_SUMMARY_CHARS, instructions)
        self.assertNotIn("🟣" * (MAX_JOB_CONTINUITY_SUMMARY_CHARS + 1), instructions)
        await app.stop()

    async def test_prior_summary_is_excluded_from_authoritative_outcome_bundle(self):
        backend = PriorContextCannotTerminalizeBackend()
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(self.store.create_job("Verify X").id)
        await app.work_current_job_once()
        outcome = await app.work_current_job_once()
        self.assertEqual(outcome.disposition.value, "continue")
        self.assertIs(self.store.get_run(binding.run.id).status, JobRunStatus.RUNNING)
        self.assertIn("Condition X is currently satisfied", backend.requests[2][1])
        self.assertNotIn("Condition X is currently satisfied", backend.requests[3][1])
        await app.stop()

    async def test_new_occurrence_does_not_inherit_terminalized_summary(self):
        backend = SummaryBackend((
            ("continue", "old occurrence"), ("continue", "new occurrence"),
        ))
        app = self.app(backend)
        await app.start()
        job = self.store.create_job("Daily")
        app.start_job_run(job.id)
        await app.work_current_job_once()
        app.finish_job_run(JobRunStatus.STOPPED, "stopped")
        app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertNotIn(HEADING, backend.requests[2][1])
        self.assertNotIn("old occurrence", backend.requests[2][1])
        await app.stop()

    async def test_pause_resume_keeps_summary_but_not_active_goal(self):
        timer = FakeTimer()
        backend = SummaryBackend((
            ("continue", "resume here"), ("continue", "later"),
        ))
        app = self.app(backend, timer=timer)
        await app.start()
        app.start_job_run(self.store.create_job("Pause").id)
        await app.work_current_job_once()
        old_goal = app.active_goal
        app.pause_task()
        await self._tick(timer)
        self.assertEqual(app.job_continuation.last_summary, "resume here")
        app.resume_task()
        self.assertIsNot(app.active_goal, old_goal)
        await self._tick(timer)
        self.assertIn("resume here", backend.requests[2][1])
        await app.stop()
