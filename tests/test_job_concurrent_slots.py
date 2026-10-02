import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import (
    ApplicationOptions, JOB_OUTCOME_EVALUATION_REQUEST, REPORT_JOB_OUTCOME_TOOL,
    RobotApplication,
)
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import JobRunStatus, SQLiteJobStore
from embodied_runtime.jobs import (
    JobContinuation, JobContinuationReadiness, JobContinuationState,
    JobReadinessEventType, JobWakeEvent,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.resources import ResourceBusyError, ResourceKey
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


class ConcurrentBackend(TextCognitionBackend):
    identifier = "concurrent-test"

    def __init__(self):
        self.started = {}
        self.release = {}
        self.outstanding = set()
        self.operator_started = asyncio.Event()
        self.operator_release = asyncio.Event()
        self.operator_tools = ()

    def gate(self, source):
        self.started[source] = asyncio.Event()
        self.release[source] = asyncio.Event()

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        text = instructions or ""
        source = next((key for key in self.started if f"source: {key}" in text), None)
        if source is not None and message != JOB_OUTCOME_EVALUATION_REQUEST:
            self.outstanding.add(source)
            self.started[source].set()
            try:
                await self.release[source].wait()
            finally:
                self.outstanding.discard(source)
            return f"work {source}"
        if message == "operator question":
            self.operator_tools = tuple(tool.name for tool in tools)
            self.operator_started.set()
            await self.operator_release.wait()
            return "operator answer"
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name,
                json.dumps({"disposition": "completed", "summary": "done",
                            "report": None, "readiness": None,
                            "delay_seconds": None}),
            ))
        return "ok"


class ParkingBackend(ConcurrentBackend):
    def __init__(self):
        super().__init__()
        self.outcomes = {}

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            text = instructions or ""
            source = next((key for key in self.started if f"source: {key}" in text), None)
            count = self.outcomes.get(source, 0)
            self.outcomes[source] = count + 1
            await tool_executor(CognitionToolCall(
                REPORT_JOB_OUTCOME_TOOL.name,
                json.dumps({
                    "disposition": "continue" if count == 0 else "completed",
                    "summary": f"summary {source}", "report": None,
                    "readiness": "after_delay" if count == 0 else None,
                    "delay_seconds": 1 if count == 0 else None,
                }),
            ))
            return "outcome"
        return await super().respond(
            message, instructions=instructions, tools=tools,
            tool_executor=tool_executor,
            refreshed_instructions=refreshed_instructions,
        )


class ConcurrentJobSlotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.temp.cleanup()

    def app(self, backend, capacity):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True,
                               jobs_max_concurrent_work=capacity),
            platform_provider=Platform(), cognition_backend=backend,
            job_store=self.store,
            wall_clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
        )

    async def test_two_exact_job_tasks_overlap_out_of_order_and_fill_freed_slot(self):
        backend = ConcurrentBackend()
        app = self.app(backend, 2)
        await app.start()
        bindings = [app.start_job_run(self.store.create_job(name).id)
                    for name in ("A", "B", "C")]
        contexts = [app._context_for_run(binding.run.id) for binding in bindings]
        sources = [f"JOB{binding.job.id}/RUN{binding.run.id}" for binding in bindings]
        for source in sources:
            backend.gate(source)

        first = asyncio.create_task(app.work_job_run_once(bindings[0].run.id))
        await backend.started[sources[0]].wait()
        second = asyncio.create_task(app.work_job_run_once(bindings[1].run.id))
        await backend.started[sources[1]].wait()
        self.assertEqual(backend.outstanding, set(sources[:2]))
        self.assertEqual(app.job_work_slots_occupied, 2)
        self.assertEqual(app.job_work_slots_available, 0)
        self.assertIsNot(contexts[0].active_work_task, contexts[1].active_work_task)
        with self.assertRaisesRegex(RuntimeError, "compatibility view ambiguous"):
            _ = app._active_job_work_task
        self.assertEqual(len(app.episode_coordinator.current_autonomous_episodes), 2)
        self.assertIs(contexts[0].task_binding.active_goal,
                      app._context_for_run(bindings[0].run.id).task_binding.active_goal)
        with self.assertRaisesRegex(RuntimeError, "capacity unavailable"):
            await app.work_job_run_once(bindings[2].run.id)

        backend.release[sources[1]].set()
        await second
        self.assertEqual(app.job_work_slots_available, 1)
        third = asyncio.create_task(app.work_job_run_once(bindings[2].run.id))
        await backend.started[sources[2]].wait()
        self.assertEqual(backend.outstanding, {sources[0], sources[2]})
        backend.release[sources[0]].set()
        await first
        backend.release[sources[2]].set()
        await third
        self.assertEqual(app.job_work_slots_occupied, 0)
        self.assertEqual(app.job_execution_contexts, ())
        await app.stop()

    async def test_four_exact_job_requests_fill_deployment_capacity(self):
        backend = ConcurrentBackend()
        app = self.app(backend, 4)
        await app.start()
        bindings = [app.start_job_run(self.store.create_job(str(index)).id)
                    for index in range(1, 6)]
        sources = [f"JOB{binding.job.id}/RUN{binding.run.id}" for binding in bindings]
        for source in sources:
            backend.gate(source)
        tasks = [asyncio.create_task(app.work_job_run_once(binding.run.id))
                 for binding in bindings[:4]]
        await asyncio.gather(*(backend.started[source].wait()
                               for source in sources[:4]))
        self.assertEqual(backend.outstanding, set(sources[:4]))
        self.assertEqual((app.job_work_slots_occupied,
                          app.job_work_slots_available), (4, 0))
        with self.assertRaisesRegex(RuntimeError, "capacity unavailable"):
            await app.work_job_run_once(bindings[4].run.id)
        backend.release[sources[0]].set()
        await tasks[0]
        fifth = asyncio.create_task(app.work_job_run_once(bindings[4].run.id))
        await backend.started[sources[4]].wait()
        self.assertEqual(app.job_work_slots_occupied, 4)
        for source in sources[1:]:
            backend.release[source].set()
        await asyncio.gather(*tasks[1:], fifth)
        await app.stop()

    async def test_operator_overlaps_two_jobs_without_consuming_slot(self):
        backend = ConcurrentBackend()
        app = self.app(backend, 2)
        await app.start()
        bindings = [app.start_job_run(self.store.create_job(name).id)
                    for name in ("A", "B")]
        sources = [f"JOB{binding.job.id}/RUN{binding.run.id}" for binding in bindings]
        for source in sources:
            backend.gate(source)
        works = [asyncio.create_task(app.work_job_run_once(binding.run.id))
                 for binding in bindings]
        await asyncio.gather(*(backend.started[source].wait() for source in sources))
        operator = asyncio.create_task(app.request_cognition(
            "operator question", source="voice"))
        await backend.operator_started.wait()
        self.assertEqual(app.job_work_slots_occupied, 2)
        self.assertEqual(len(app.episode_coordinator.current_autonomous_episodes), 2)
        self.assertEqual(app.episode_coordinator.current_operator.trigger_source, "voice")
        self.assertNotIn("set_goal", backend.operator_tools)
        self.assertNotIn("resolve_goal", backend.operator_tools)
        backend.operator_release.set()
        self.assertEqual(await operator, "operator answer")
        self.assertTrue(all(app._context_for_run(binding.run.id) is not None
                            for binding in bindings))
        for source in reversed(sources):
            backend.release[source].set()
        await asyncio.gather(*works)
        await app.stop()

    async def test_capacity_one_rejects_second_until_exact_cleanup(self):
        backend = ConcurrentBackend()
        app = self.app(backend, 1)
        await app.start()
        first_binding = app.start_job_run(self.store.create_job("A").id)
        second_binding = app.start_job_run(self.store.create_job("B").id)
        first_source = f"JOB{first_binding.job.id}/RUN{first_binding.run.id}"
        second_source = f"JOB{second_binding.job.id}/RUN{second_binding.run.id}"
        backend.gate(first_source)
        backend.gate(second_source)
        first = asyncio.create_task(app.work_job_run_once(first_binding.run.id))
        await backend.started[first_source].wait()
        with self.assertRaisesRegex(RuntimeError, "capacity unavailable"):
            await app.work_job_run_once(second_binding.run.id)
        backend.release[first_source].set()
        await first
        second = asyncio.create_task(app.work_job_run_once(second_binding.run.id))
        await backend.started[second_source].wait()
        backend.release[second_source].set()
        await second
        await app.stop()

    async def test_shutdown_cancels_all_exact_blocked_tasks(self):
        backend = ConcurrentBackend()
        app = self.app(backend, 2)
        await app.start()
        bindings = [app.start_job_run(self.store.create_job(name).id)
                    for name in ("A", "B")]
        sources = [f"JOB{binding.job.id}/RUN{binding.run.id}" for binding in bindings]
        for source in sources:
            backend.gate(source)
        tasks = [asyncio.create_task(app.work_job_run_once(binding.run.id))
                 for binding in bindings]
        await asyncio.gather(*(backend.started[source].wait() for source in sources))
        await app.stop()
        self.assertTrue(all(task.done() for task in tasks))
        self.assertEqual(app.job_execution_contexts, ())
        self.assertFalse(app.episode_coordinator.current_autonomous_episodes)
        verification = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")
        self.addCleanup(verification.close)
        self.assertTrue(all(verification.get_run(binding.run.id).status
                            is JobRunStatus.INTERRUPTED for binding in bindings))

    async def test_exact_stop_cancels_only_selected_active_work(self):
        backend = ConcurrentBackend()
        app = self.app(backend, 2)
        await app.start()
        bindings = [app.start_job_run(self.store.create_job(name).id)
                    for name in ("A", "B")]
        sources = [f"JOB{item.job.id}/RUN{item.run.id}" for item in bindings]
        for source in sources:
            backend.gate(source)
        works = [asyncio.create_task(app.work_job_run_once(item.run.id))
                 for item in bindings]
        await asyncio.gather(*(backend.started[source].wait() for source in sources))

        stopped = await app.finish_job_run_by_id(
            bindings[0].run.id, JobRunStatus.STOPPED, "operator stop")

        self.assertIs(stopped.run.status, JobRunStatus.STOPPED)
        self.assertTrue(works[0].cancelled())
        self.assertIsNone(app._context_for_run(bindings[0].run.id))
        context_b = app._context_for_run(bindings[1].run.id)
        self.assertIsNotNone(context_b)
        self.assertIs(context_b.active_work_task, works[1])
        self.assertIn(sources[1], backend.outstanding)
        self.assertEqual(len(app.episode_coordinator.current_autonomous_episodes), 1)
        backend.release[sources[1]].set()
        await works[1]
        await app.stop()

    async def test_exact_complete_fail_and_selectorless_ambiguity(self):
        app = self.app(ConcurrentBackend(), 2)
        await app.start()
        jobs = [self.store.create_job(name) for name in ("A", "B", "C")]
        bindings = [app.start_job_run(job.id) for job in jobs[:2]]
        with self.assertRaisesRegex(RuntimeError, "ambiguous; specify RUN<n>"):
            app.finish_job_run(JobRunStatus.STOPPED)
        completed = await app.finish_job_run_by_id(
            bindings[0].run.id, JobRunStatus.COMPLETED, "done")
        failed = await app.finish_job_run_by_id(
            bindings[1].run.id, JobRunStatus.FAILED, "failed")
        self.assertIs(completed.run.status, JobRunStatus.COMPLETED)
        self.assertIs(failed.run.status, JobRunStatus.FAILED)
        self.assertEqual(self.store.get_run(failed.run.id).error_summary, "failed")

        live = app.start_job_run(jobs[2].id)
        self.store.set_job_enabled(jobs[2].id, False)
        self.assertIsNotNone(app._context_for_run(live.run.id))
        self.assertFalse(self.store.get_job(jobs[2].id).enabled)
        stopped = app.finish_job_run(JobRunStatus.STOPPED)
        self.assertEqual(stopped.run.id, live.run.id)
        await app.stop()

    async def test_two_contexts_retain_independent_parked_continuations(self):
        backend = ParkingBackend()
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True, jobs_auto_continue=True,
                               jobs_max_concurrent_work=2),
            platform_provider=Platform(), cognition_backend=backend,
            job_store=self.store,
            wall_clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
            job_continuation_sleep=lambda _: asyncio.Event().wait(),
        )
        await app.start()
        bindings = [app.start_job_run(self.store.create_job(name).id)
                    for name in ("A", "B")]
        sources = [f"JOB{binding.job.id}/RUN{binding.run.id}" for binding in bindings]
        for source in sources:
            backend.gate(source)
        works = [asyncio.create_task(app.work_job_run_once(binding.run.id))
                 for binding in bindings]
        await asyncio.gather(*(backend.started[source].wait() for source in sources))
        for source in sources:
            backend.release[source].set()
        await asyncio.gather(*works)
        contexts = [app._context_for_run(binding.run.id) for binding in bindings]
        self.assertTrue(all(context.execution_state == "parked" for context in contexts))
        self.assertEqual(
            {context.continuation.last_summary for context in contexts},
            {f"summary {source}" for source in sources},
        )
        self.assertTrue(all(context.continuation.automatic_steps_remaining == 3
                            for context in contexts))
        self.assertTrue(all(self.store.get_run(binding.run.id).status
                            is JobRunStatus.RUNNING for binding in bindings))
        await app.stop()

    async def test_one_event_satisfies_all_matching_exact_contexts(self):
        backend = ConcurrentBackend()
        app = self.app(backend, 1)
        await app.start()
        bindings = [app.start_job_run(self.store.create_job(name).id)
                    for name in ("A", "B", "C")]
        contexts = [app._context_for_run(binding.run.id) for binding in bindings]
        for index, context in enumerate(contexts):
            context.continuation = JobContinuation(
                context.job_id, context.run_id, context.task_id,
                JobContinuationState.ARMED, 3, f"summary-{index}",
                JobContinuationReadiness.WAIT_FOR_EVENT,
                event_type=(JobReadinessEventType.PRESENCE_CHANGED
                            if index < 2 else JobReadinessEventType.POWER_RECOVERED),
                event_armed_after_ns=100,
            )
        matched = app._satisfy_job_event(
            JobReadinessEventType.PRESENCE_CHANGED, 101,
            JobWakeEvent(JobReadinessEventType.PRESENCE_CHANGED, present=True),
        )
        self.assertTrue(matched)
        self.assertTrue(contexts[0].continuation.event_satisfied)
        self.assertTrue(contexts[1].continuation.event_satisfied)
        self.assertFalse(contexts[2].continuation.event_satisfied)
        await app.stop()

    async def test_diagnostics_report_ambiguous_live_contexts_as_active(self):
        app = self.app(ConcurrentBackend(), 2)
        await app.start()
        for name in ("A", "B"):
            app.start_job_run(self.store.create_job(name).id)
        app._foreground_job_run_id = None
        diagnostic = app._diagnostic_job_runtime()
        self.assertEqual(diagnostic["status"], "ok")
        self.assertEqual(diagnostic["job_state"], "multiple_live_contexts")
        self.assertIsNone(diagnostic["current_job"])
        self.assertEqual(len(diagnostic["live_contexts"]), 2)
        await app.stop()

    async def test_exact_task_resource_owners_contend_without_stealing(self):
        app = self.app(ConcurrentBackend(), 2)
        await app.start()
        bindings = [app.start_job_run(self.store.create_job(name).id)
                    for name in ("A", "B")]
        contexts = [app._context_for_run(binding.run.id) for binding in bindings]
        self.assertNotEqual(contexts[0].resource_owner, contexts[1].resource_owner)
        shared = ResourceKey("camera")
        independent = ResourceKey("body")
        tokens = []
        try:
            token = app._job_execution_context.set(contexts[0])
            tokens.append(token)
            lease_a = app.acquire_task_resource(shared)
            app._job_execution_context.reset(tokens.pop())
            token = app._job_execution_context.set(contexts[1])
            tokens.append(token)
            lease_b = app.acquire_task_resource(independent)
            with self.assertRaises(ResourceBusyError):
                app.acquire_task_resource(shared)
            with self.assertRaisesRegex(RuntimeError, "not owned"):
                app.release_task_resource(lease_a)
            app._job_execution_context.reset(tokens.pop())
            app.resources.release_all(contexts[0].resource_owner)
            self.assertEqual(app.resources.lease_for(independent).owner,
                             contexts[1].resource_owner)
            token = app._job_execution_context.set(contexts[1])
            tokens.append(token)
            app.release_task_resource(lease_b)
        finally:
            while tokens:
                app._job_execution_context.reset(tokens.pop())
        await app.stop()
