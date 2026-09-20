import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from embodied_runtime.app import (
    JOB_OUTCOME_EVALUATION_REQUEST, REPORT_JOB_OUTCOME_TOOL,
    ApplicationOptions, RobotApplication,
)
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import JobRunStatus, JobWorkDisposition, SQLiteJobStore
from embodied_runtime.profile import RobotProfile
from embodied_runtime.tasks import TaskStatus
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


class JobBackend(TextCognitionBackend):
    identifier = "job-test"

    def __init__(self, disposition="continue", *, outcome_call=True):
        self.disposition = disposition
        self.outcome_call = outcome_call
        self.requests = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tuple(tool.name for tool in tools)))
        if message == JOB_OUTCOME_EVALUATION_REQUEST and self.outcome_call:
            readiness = "ready" if self.disposition == "continue" else None
            await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name,
                json.dumps({"disposition": self.disposition,
                            "summary": "bounded result", "readiness": readiness,
                            "delay_seconds": None}),
            ))
        return "bounded work response"


class OutcomeProposalBackend(JobBackend):
    def __init__(self, disposition="completed", *, after_tool=None,
                 fail_after_tool=False):
        super().__init__(disposition)
        self.after_tool = after_tool
        self.fail_after_tool = fail_after_tool
        self.tool_result = None

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tuple(tool.name for tool in tools)))
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            self.tool_result = await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name,
                json.dumps({"disposition": self.disposition, "summary": "done",
                            "readiness": "ready" if self.disposition == "continue" else None,
                            "delay_seconds": None}),
            ))
            if self.after_tool is not None:
                self.after_tool()
            if self.fail_after_tool:
                raise RuntimeError("provider failed after tool")
        return "bounded work response"


class BlockingBackend(JobBackend):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def respond(self, message, **kwargs):
        self.started.set()
        await asyncio.Event().wait()


class FirstRequestBlocksBackend(JobBackend):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tuple(tool.name for tool in tools)))
        if len(self.requests) == 1:
            self.started.set()
            await self.release.wait()
        elif message == JOB_OUTCOME_EVALUATION_REQUEST:
            await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name,
                '{"disposition":"continue","summary":"more work",'
                '"readiness":"ready","delay_seconds":null}',
            ))
        return "bounded response"


class FakeTimer:
    def __init__(self):
        self.now = 100.0
        self.waiters = []

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((delay, future))
        await future

    async def advance(self):
        await asyncio.sleep(0)
        delay, future = self.waiters.pop(0)
        self.now += delay
        future.set_result(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)


class JobExecutionTests(unittest.IsolatedAsyncioTestCase):
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

    def app(self, backend, **kwargs):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True,
                               initiative_goal_closure_enabled=True),
            platform_provider=Platform(), cognition_backend=backend,
            job_store=self.store,
            wall_clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
            **kwargs,
        )

    async def test_one_explicit_continue_episode_is_grounded_and_does_not_repeat(self):
        backend = JobBackend()
        job = self.store.create_job("Review logs", "Full durable description")
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        memory = app.working_memory.snapshot()

        outcome = await app.work_current_job_once()

        self.assertIs(outcome.disposition, JobWorkDisposition.CONTINUE)
        self.assertEqual(len(backend.requests), 2)
        initial = backend.requests[0]
        self.assertIn("Full durable description", initial[1])
        self.assertIn("kind: job_run_work", initial[1])
        self.assertIn("source: JOB1/RUN1", initial[1])
        self.assertNotIn("schedule_followup", initial[2])
        self.assertEqual(binding.task.goal.description, "Complete JOB1: Review logs")
        self.assertIs(app.current_job_run, binding)
        self.assertIs(app.current_task, binding.task)
        self.assertIs(app.active_goal, app._current_task_binding.active_goal)
        self.assertEqual(app.working_memory.snapshot(), memory)
        self.assertIsNone(app.episode_coordinator.current)
        await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 2)

        await app.work_current_job_once()
        self.assertEqual(len(backend.requests), 4)
        await app.stop()

    async def test_completed_uses_authoritative_job_and_task_terminal_path(self):
        app = None

        def assert_still_running_during_callback():
            self.assertIs(app.current_job_run.run.status, JobRunStatus.RUNNING)
            self.assertIs(app.current_task.status, TaskStatus.RUNNING)
            self.assertIsNotNone(app.active_goal)

        backend = OutcomeProposalBackend(
            "completed", after_tool=assert_still_running_during_callback
        )
        job = self.store.create_job("Finish")
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        outcome = await app.work_current_job_once()
        self.assertIs(outcome.disposition, JobWorkDisposition.COMPLETED)
        self.assertIsNone(app.current_job_run)
        self.assertIsNone(app.current_task)
        self.assertIsNone(app.active_goal)
        run = self.store.get_run(binding.run.id)
        self.assertIs(run.status, JobRunStatus.COMPLETED)
        self.assertEqual(run.outcome_summary, "done")
        self.assertIn('"status": "accepted"', backend.tool_result.output)
        self.assertIn('"commit": "after_outcome_evaluation"',
                      backend.tool_result.output)
        self.assertNotIn("complete_goal", backend.requests[-1][2])
        await app.stop()

    async def test_backend_failure_after_accepted_proposal_does_not_commit(self):
        app = None

        def assert_still_running_during_callback():
            self.assertIs(app.current_job_run.run.status, JobRunStatus.RUNNING)
            self.assertIs(app.current_task.status, TaskStatus.RUNNING)

        backend = OutcomeProposalBackend(
            after_tool=assert_still_running_during_callback, fail_after_tool=True
        )
        job = self.store.create_job("Remain running")
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        goal = app.active_goal
        with self.assertRaisesRegex(RuntimeError, "provider failed after tool"):
            await app.work_current_job_once()
        self.assertIs(app.current_job_run, binding)
        self.assertIs(app.current_task, binding.task)
        self.assertIs(app.active_goal, goal)
        self.assertIs(self.store.get_run(binding.run.id).status, JobRunStatus.RUNNING)
        await app.stop()

    async def test_terminal_proposal_stale_before_commit_becomes_continue(self):
        app = None
        backend = OutcomeProposalBackend(after_tool=lambda: app.pause_task())
        job = self.store.create_job("Become stale")
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        outcome = await app.work_current_job_once()
        self.assertIs(outcome.disposition, JobWorkDisposition.CONTINUE)
        self.assertIs(self.store.get_run(binding.run.id).status, JobRunStatus.RUNNING)
        self.assertIs(app.current_task.status, TaskStatus.PAUSED)
        self.assertIsNone(app.active_goal)
        await app.stop()

    async def test_failed_maps_summary_to_error(self):
        backend = JobBackend("failed")
        job = self.store.create_job("Fail")
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        outcome = await app.work_current_job_once()
        self.assertIs(outcome.disposition, JobWorkDisposition.FAILED)
        run = self.store.get_run(binding.run.id)
        self.assertIs(run.status, JobRunStatus.FAILED)
        self.assertEqual(run.error_summary, "bounded result")
        await app.stop()

    async def test_no_or_invalid_outcome_call_conservatively_continues(self):
        backend = JobBackend(outcome_call=False)
        app = self.app(backend)
        await app.start()
        for disposition, call in (("continue", False), ("stopped", True)):
            with self.subTest(disposition=disposition, call=call):
                backend.disposition = disposition
                backend.outcome_call = call
                job = self.store.create_job(f"Work {disposition} {call}")
                binding = app.start_job_run(job.id)
                outcome = await app.work_current_job_once()
                self.assertIs(outcome.disposition, JobWorkDisposition.CONTINUE)
                self.assertIs(app.current_job_run, binding)
                self.assertIs(app.current_task.status, TaskStatus.RUNNING)
                app.finish_job_run(JobRunStatus.STOPPED)
        await app.stop()

    async def test_paused_fails_before_cognition_and_resume_allows_work(self):
        backend = JobBackend()
        job = self.store.create_job("Paused")
        app = self.app(backend)
        await app.start()
        app.start_job_run(job.id)
        app.pause_task()
        with self.assertRaisesRegex(RuntimeError, "paused"):
            await app.work_current_job_once()
        self.assertEqual(backend.requests, [])
        app.resume_task()
        await app.work_current_job_once()
        self.assertEqual(len(backend.requests), 2)
        await app.stop()

    async def test_job_episode_closes_before_deferred_temporal_release(self):
        backend = FirstRequestBlocksBackend()
        timer = FakeTimer()
        job = self.store.create_job("Release due work")
        app = self.app(
            backend, temporal_sleep=timer.sleep, monotonic_clock=lambda: timer.now
        )
        await app.start()
        app.start_job_run(job.id)
        goal = app.active_goal
        app.temporal.schedule(10, "independent due work", goal)
        closed = []
        original_close = app.episode_coordinator.close

        def record_close(episode, reason):
            result = original_close(episode, reason)
            closed.append(result)
            return result

        app.episode_coordinator.close = record_close
        work = asyncio.create_task(app.work_current_job_once())
        await backend.started.wait()
        self.assertEqual(app.episode_coordinator.current.trigger_kind, "job_run")
        await timer.advance()
        self.assertEqual(app.temporal_followup_status().state, "due_pending")
        backend.release.set()
        outcome = await work
        self.assertIs(outcome.disposition, JobWorkDisposition.CONTINUE)
        while app.attention.status().state == "in_flight":
            await asyncio.sleep(0)
        self.assertEqual(
            [(episode.trigger_kind, episode.completion_reason) for episode in closed],
            [("job_run", "no_action"), ("temporal_followup_due", "no_action")],
        )
        self.assertEqual(len(backend.requests), 3)
        self.assertEqual(
            [request[1].count("kind: job_run_work") for request in backend.requests],
            [1, 1, 0],
        )
        await app.stop()

    async def test_shutdown_job_cancellation_does_not_release_temporal_work(self):
        backend = BlockingBackend()
        job = self.store.create_job("Shutdown")
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        app.attention.release_temporal_due = AsyncMock()
        work = asyncio.create_task(app.work_current_job_once())
        await backend.started.wait()
        await app.stop()
        with self.assertRaises(asyncio.CancelledError):
            await work
        app.attention.release_temporal_due.assert_not_awaited()
        reopened = SQLiteJobStore(self.path)
        try:
            self.assertIs(reopened.get_run(binding.run.id).status, JobRunStatus.RUNNING)
        finally:
            reopened.close()
