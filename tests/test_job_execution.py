import asyncio
import json
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from embodied_runtime.app import (
    JOB_OUTCOME_EVALUATION_REQUEST, REPORT_JOB_OUTCOME_TOOL,
    ApplicationOptions, JobExecutionContext, RobotApplication,
    _CurrentTaskBinding,
)
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    MAX_RUN_REPORT_CHARS, JobRunStatus, JobWorkDisposition, SQLiteJobStore,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.tasks import Task, TaskStatus
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
                            "summary": "bounded result", "report": None,
                            "readiness": readiness,
                            "delay_seconds": None}),
            ))
        return "bounded work response"


class OutcomeProposalBackend(JobBackend):
    def __init__(self, disposition="completed", *, after_tool=None,
                 fail_after_tool=False, report=None):
        super().__init__(disposition)
        self.after_tool = after_tool
        self.fail_after_tool = fail_after_tool
        self.tool_result = None
        self.report = report

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tuple(tool.name for tool in tools)))
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            self.tool_result = await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name,
                json.dumps({"disposition": self.disposition, "summary": "done",
                            "report": self.report,
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
                '{"disposition":"continue","summary":"more work","report":null,'
                '"readiness":"ready","delay_seconds":null}',
            ))
        return "bounded response"


class OverlappingOperatorBackend(JobBackend):
    def __init__(self):
        super().__init__("completed")
        self.job_started = asyncio.Event()
        self.operator_started = asyncio.Event()
        self.release_operator = asyncio.Event()
        self.release_job = asyncio.Event()
        self.job_outstanding = False
        self.operator_tools = None

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tuple(tool.name for tool in tools)))
        if "kind: job_run_work" in (instructions or "") and not self.job_started.is_set():
            self.job_outstanding = True
            self.job_started.set()
            await self.release_job.wait()
            self.job_outstanding = False
            return "job work"
        if message == "what's your current voltage":
            self.assert_job_outstanding = self.job_outstanding
            self.operator_tools = tuple(tool.name for tool in tools)
            self.operator_started.set()
            await self.release_operator.wait()
            return "operator answer"
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name,
                '{"disposition":"completed","summary":"done","report":null,'
                '"readiness":null,"delay_seconds":null}',
            ))
        return "bounded response"


class ContinuingOverlapBackend(JobBackend):
    def __init__(self):
        super().__init__()
        self.first_job_started = asyncio.Event()
        self.release_first_job = asyncio.Event()
        self.operator_started = asyncio.Event()
        self.release_operator = asyncio.Event()
        self.job_initial_instructions = []
        self.outcome_requests = 0

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tuple(tool.name for tool in tools)))
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            self.outcome_requests += 1
            disposition = (
                "continue" if self.outcome_requests == 1 else "completed"
            )
            readiness = "ready" if disposition == "continue" else None
            await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name,
                json.dumps({"disposition": disposition, "summary": "bounded",
                            "report": None, "readiness": readiness,
                            "delay_seconds": None}),
            ))
            return "bounded response"
        if "kind: job_run_work" in (instructions or ""):
            self.job_initial_instructions.append(instructions)
            if len(self.job_initial_instructions) == 1:
                self.first_job_started.set()
                await self.release_first_job.wait()
            return "job work"
        if message == "shared turn B":
            self.operator_started.set()
            await self.release_operator.wait()
            return "operator answer B"
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

    async def test_context_rejects_initially_mismatched_task_binding(self):
        app = self.app(JobBackend())
        await app.start()
        binding = app.start_job_run(self.store.create_job("Exact identity").id)

        with self.assertRaisesRegex(
            ValueError, "Task binding must match its JobRun Task"
        ):
            JobExecutionContext(
                binding, _CurrentTaskBinding(Task("Different Task"), None)
            )

        await app.stop()

    async def test_one_explicit_continue_episode_is_grounded_and_does_not_repeat(self):
        backend = JobBackend()
        job = self.store.create_job("Review logs", "Full durable description")
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        memory = app.working_memory.snapshot()

        outcome = await app.work_current_job_once()

        self.assertIs(outcome.disposition, JobWorkDisposition.CONTINUE)
        metrics = app.observability.snapshot()["metrics"]
        self.assertEqual((metrics["manual_job_work"],
                          metrics["automatic_job_work"]), (1, 0))
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
        self.assertIsNone(app.episode_coordinator.current_autonomous)
        await asyncio.sleep(0)
        self.assertEqual(len(backend.requests), 2)

        await app.work_current_job_once()
        self.assertEqual(len(backend.requests), 4)
        await app.stop()

    async def test_operator_provider_request_overlaps_job_and_preserves_binding(self):
        backend = OverlappingOperatorBackend()
        job = self.store.create_job("Check system")
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        task_binding = app._current_task_binding
        active_goal = app.active_goal
        context = app.current_job_execution_context
        self.assertIsNotNone(context)
        self.assertEqual((context.job_id, context.run_id, context.task_id),
                         (binding.job.id, binding.run.id, binding.task.id))
        self.assertEqual(len(app.job_execution_contexts), 1)
        self.assertNotIn("working_memory", {field.name for field in fields(context)})

        work = asyncio.create_task(app.work_current_job_once())
        await backend.job_started.wait()
        autonomous = app.episode_coordinator.current_autonomous
        operator = asyncio.create_task(app.request_cognition(
            "what's your current voltage", source="voice"
        ))
        await backend.operator_started.wait()

        self.assertTrue(backend.assert_job_outstanding)
        self.assertNotIn("set_goal", backend.operator_tools)
        self.assertNotIn("resolve_goal", backend.operator_tools)
        self.assertIs(app.current_job_run, binding)
        self.assertIs(app._current_task_binding, task_binding)
        self.assertIs(app.active_goal, active_goal)
        self.assertIs(app.episode_coordinator.current_autonomous, autonomous)
        self.assertEqual(app.episode_coordinator.current_operator.trigger_source, "voice")
        self.assertNotEqual(
            app.episode_coordinator.current_operator.id, autonomous.id
        )
        backend.release_operator.set()
        self.assertEqual(await operator, "operator answer")
        self.assertNotIn("what's your current voltage", backend.requests[0][1])
        self.assertEqual(app.working_memory.snapshot()[-1].operator_text,
                         "what's your current voltage")
        self.assertIs(app.current_job_run, binding)
        self.assertIs(app._current_task_binding, task_binding)
        self.assertIs(app.active_goal, active_goal)
        self.assertIs(app._active_job_work_task, work)
        self.assertIs(context.active_work_task, work)
        self.assertEqual(sum(item.active_work_task is not None
                             for item in app.job_execution_contexts), 1)

        backend.release_job.set()
        outcome = await work
        self.assertIs(outcome.disposition, JobWorkDisposition.COMPLETED)
        self.assertIsNone(app.current_job_run)
        self.assertEqual(app.job_execution_contexts, ())
        self.assertIs(self.store.get_run(binding.run.id).status, JobRunStatus.COMPLETED)
        later = self.store.create_job("See later shared memory")
        app.start_job_run(later.id)
        later_request_index = len(backend.requests)
        await app.work_current_job_once()
        self.assertIn("what's your current voltage",
                      backend.requests[later_request_index][1])
        self.assertEqual(len(app.working_memory.snapshot()), 1)
        await app.stop()

    async def test_same_job_context_sees_operator_turn_only_on_next_episode(self):
        backend = ContinuingOverlapBackend()
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(self.store.create_job("Continue exactly").id)
        context = app.current_job_execution_context
        memory_before = app.working_memory.snapshot()

        first_work = asyncio.create_task(app.work_current_job_once())
        await backend.first_job_started.wait()
        operator = asyncio.create_task(app.request_cognition(
            "shared turn B", source="voice"
        ))
        await backend.operator_started.wait()
        backend.release_operator.set()
        self.assertEqual(await operator, "operator answer B")
        self.assertEqual(len(app.working_memory.snapshot()), len(memory_before) + 1)
        self.assertNotIn("shared turn B", backend.job_initial_instructions[0])

        backend.release_first_job.set()
        first = await first_work
        self.assertIs(first.disposition, JobWorkDisposition.CONTINUE)
        self.assertIs(app.current_job_execution_context, context)
        self.assertEqual((app.current_job_run.run.id, app.current_task.id),
                         (binding.run.id, binding.task.id))

        second = await app.work_current_job_once()

        self.assertIs(second.disposition, JobWorkDisposition.COMPLETED)
        self.assertIn("shared turn B", backend.job_initial_instructions[1])
        self.assertEqual(len(app.working_memory.snapshot()), len(memory_before) + 1)
        await app.stop()

    async def test_terminal_context_retains_capacity_until_exact_task_cleanup(self):
        app = None
        old_context = None
        replacement = None
        rejected_task = None

        def inspect_terminal_unwind():
            nonlocal replacement, rejected_task
            app.finish_job_run = finish_job_run
            self.assertEqual(old_context.execution_state, "terminal")
            self.assertIs(old_context.active_work_task, work)
            self.assertIn(old_context, app.job_execution_contexts)
            self.assertIs(app._active_job_work_task, work)

            second_job = self.store.create_job("Replacement occurrence")
            app.start_job_run(second_job.id)
            replacement = app.current_job_execution_context
            rejected_task = asyncio.create_task(asyncio.sleep(10))
            with self.assertRaisesRegex(
                RuntimeError, "another Job work episode is already active"
            ):
                app._active_job_work_task = rejected_task
            rejected_task.cancel()
            self.assertIs(app._active_job_work_task, work)

        backend = OutcomeProposalBackend("completed")
        app = self.app(backend)
        await app.start()
        app.start_job_run(self.store.create_job("Terminal owner").id)
        old_context = app.current_job_execution_context
        finish_job_run = app.finish_job_run

        def finish_and_inspect(*args, **kwargs):
            finished = finish_job_run(*args, **kwargs)
            inspect_terminal_unwind()
            return finished

        app.finish_job_run = finish_and_inspect
        work = asyncio.create_task(app.work_current_job_once())

        outcome = await work

        self.assertIs(outcome.disposition, JobWorkDisposition.COMPLETED)
        self.assertIsNone(old_context.active_work_task)
        self.assertNotIn(old_context, app.job_execution_contexts)
        self.assertIs(app.current_job_execution_context, replacement)
        self.assertIn(replacement, app.job_execution_contexts)
        self.assertIsNone(replacement.active_work_task)
        await asyncio.gather(rejected_task, return_exceptions=True)
        await app.work_current_job_once()
        await app.stop()

    async def test_shutdown_clears_context_registry_and_joins_owned_work(self):
        backend = BlockingBackend()
        job = self.store.create_job("Block until shutdown")
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        work = asyncio.create_task(app.work_current_job_once())
        await backend.started.wait()
        context = app.current_job_execution_context
        self.assertIs(context.active_work_task, work)

        await app.stop()

        self.assertTrue(work.done())
        self.assertEqual(app.job_execution_contexts, ())
        reopened = SQLiteJobStore(self.path)
        try:
            self.assertIs(reopened.get_run(binding.run.id).status,
                          JobRunStatus.INTERRUPTED)
        finally:
            reopened.close()

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

    async def test_completed_report_survives_fresh_store_and_application(self):
        report = (
            "No failed application entries were found. The previous run ended with "
            "an operator interrupt and completed cleanup with no non-daemon threads."
        )
        backend = OutcomeProposalBackend("completed", report=report)
        job = self.store.create_job("Nightly Self Log Reviewer")
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        outcome = await app.work_current_job_once()
        self.assertEqual(outcome.report, report)
        await app.stop()
        self.store.close()

        self.store = SQLiteJobStore(self.path)
        fresh_app = self.app(JobBackend(outcome_call=False))
        persisted = fresh_app.jobs.get_run(binding.run.id)
        self.assertEqual(persisted.outcome_summary, "done")
        self.assertEqual(persisted.result_report, report)

    async def test_continue_with_report_is_rejected_and_not_persisted(self):
        backend = OutcomeProposalBackend("continue", report="not terminal")
        job = self.store.create_job("Continue")
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        outcome = await app.work_current_job_once()
        self.assertIs(outcome.disposition, JobWorkDisposition.CONTINUE)
        self.assertIsNone(outcome.report)
        self.assertIsNone(self.store.get_run(binding.run.id).result_report)
        self.assertIn('"status": "rejected"', backend.tool_result.output)
        await app.stop()

    async def test_overlong_terminal_report_is_rejected_and_not_persisted(self):
        backend = OutcomeProposalBackend(
            "completed", report="x" * (MAX_RUN_REPORT_CHARS + 1)
        )
        job = self.store.create_job("Bound report")
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        outcome = await app.work_current_job_once()
        self.assertIs(outcome.disposition, JobWorkDisposition.CONTINUE)
        self.assertIs(self.store.get_run(binding.run.id).status, JobRunStatus.RUNNING)
        self.assertIsNone(self.store.get_run(binding.run.id).result_report)
        self.assertIn('"status": "rejected"', backend.tool_result.output)
        await app.stop()

    async def test_backend_failure_after_accepted_proposal_does_not_commit(self):
        app = None

        def assert_still_running_during_callback():
            self.assertIs(app.current_job_run.run.status, JobRunStatus.RUNNING)
            self.assertIs(app.current_task.status, TaskStatus.RUNNING)

        backend = OutcomeProposalBackend(
            after_tool=assert_still_running_during_callback, fail_after_tool=True,
            report="must not persist",
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
        self.assertIsNone(self.store.get_run(binding.run.id).result_report)
        await app.stop()

    async def test_terminal_proposal_stale_before_commit_becomes_continue(self):
        app = None
        backend = OutcomeProposalBackend(
            after_tool=lambda: app.pause_task(), report="stale report"
        )
        job = self.store.create_job("Become stale")
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        outcome = await app.work_current_job_once()
        self.assertIs(outcome.disposition, JobWorkDisposition.CONTINUE)
        self.assertIs(self.store.get_run(binding.run.id).status, JobRunStatus.RUNNING)
        self.assertIsNone(self.store.get_run(binding.run.id).result_report)
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
        self.assertEqual(app.episode_coordinator.current_autonomous.trigger_kind, "job_run")
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
            self.assertIs(
                reopened.get_run(binding.run.id).status, JobRunStatus.INTERRUPTED
            )
        finally:
            reopened.close()
