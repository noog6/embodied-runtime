import asyncio
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from embodied_runtime.app import ApplicationOptions, LifecycleState, RobotApplication
from embodied_runtime.console import RuntimeConsole
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import JobRunStatus, JobTriggerType, SQLiteJobStore
from embodied_runtime.profile import RobotProfile
from embodied_runtime.tasks import TaskStatus
from tests.test_job_execution import BlockingBackend, Platform
from tests.test_platform import snapshot


class FailingHardware(VirtualHardwareBackend):
    def start(self) -> None:
        raise RuntimeError("startup failed")


class TrackingHardware(VirtualHardwareBackend):
    def __init__(self):
        super().__init__()
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        super().start()

    def stop(self) -> None:
        self.stop_calls += 1
        super().stop()


class TrackingBackend(BlockingBackend):
    def __init__(self):
        super().__init__()
        self.prepare_calls = 0

    async def prepare(self) -> None:
        self.prepare_calls += 1


class FailingOperationalPlatform:
    def __init__(self):
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        if self.calls == 1:
            return snapshot()
        raise RuntimeError("operational sample failed")


class RuntimeReadyJobActivationTests(unittest.IsolatedAsyncioTestCase):
    def app(self, store, backend, *, hardware=None, platform_provider=None,
            capacity=1):
        async def sleep_forever(_delay):
            await asyncio.Event().wait()

        return RobotApplication(
            RobotProfile("test", "Test"), hardware or VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True,
                               jobs_max_concurrent_work=capacity),
            platform_provider=platform_provider or Platform(),
            cognition_backend=backend, job_store=store,
            wall_clock=lambda: datetime(2026, 9, 30, tzinfo=UTC),
            job_scheduler_sleep=sleep_forever, job_continuation_sleep=sleep_forever,
        )

    async def drain(self):
        for _ in range(20):
            await asyncio.sleep(0)

    async def test_ready_starts_normal_occurrence_and_duplicate_is_coalesced(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteJobStore(Path(temporary) / "jobs.sqlite3")
            job = store.create_job("Inspect capabilities")
            store.set_trigger(job.id, JobTriggerType.RUNTIME_READY)
            backend = BlockingBackend()
            app = self.app(store, backend)

            await app.start()
            await backend.started.wait()
            binding = app.current_job_run
            self.assertIsNotNone(binding)
            self.assertIs(binding.run.status, JobRunStatus.RUNNING)
            self.assertIs(binding.task.status, TaskStatus.RUNNING)
            self.assertIs(app.active_goal, app._current_task_binding.active_goal)
            await app._signal_runtime_ready()
            await self.drain()
            self.assertEqual(len(store.list_runs(job.id)), 1)
            await app.stop()

    async def test_disabled_or_unconfigured_job_does_not_activate(self):
        for configured, enabled in ((True, False), (False, True)):
            with self.subTest(configured=configured, enabled=enabled), \
                    tempfile.TemporaryDirectory() as temporary:
                store = SQLiteJobStore(Path(temporary) / "jobs.sqlite3")
                job = store.create_job("Inspect capabilities", enabled=enabled)
                if configured:
                    store.set_trigger(job.id, JobTriggerType.RUNTIME_READY)
                app = self.app(store, BlockingBackend())
                await app.start()
                await self.drain()
                self.assertEqual(store.list_runs(job.id), ())
                await app.stop()

    async def test_fresh_application_instance_gets_new_occurrence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite3"
            first_store = SQLiteJobStore(path)
            job = first_store.create_job("Inspect capabilities")
            first_store.set_trigger(job.id, JobTriggerType.RUNTIME_READY)
            first_backend = BlockingBackend()
            first = self.app(first_store, first_backend)
            await first.start()
            await first_backend.started.wait()
            await first.stop()

            second_store = SQLiteJobStore(path)
            second_backend = BlockingBackend()
            second = self.app(second_store, second_backend)
            await second.start()
            await second_backend.started.wait()
            runs = second_store.list_runs(job.id)
            self.assertEqual(len(runs), 2)
            self.assertIs(runs[0].status, JobRunStatus.INTERRUPTED)
            self.assertIs(runs[1].status, JobRunStatus.RUNNING)
            await second.stop()

    async def test_startup_failure_before_ready_creates_no_occurrence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite3"
            store = SQLiteJobStore(path)
            job = store.create_job("Inspect capabilities")
            store.set_trigger(job.id, JobTriggerType.RUNTIME_READY)
            app = self.app(store, BlockingBackend(), hardware=FailingHardware())
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                await app.start()
            reopened = SQLiteJobStore(path)
            self.assertEqual(reopened.list_runs(job.id), ())
            reopened.close()

    async def test_late_operational_sample_failure_does_not_activate_ready_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite3"
            store = SQLiteJobStore(path)
            job = store.create_job("Inspect capabilities")
            store.set_trigger(job.id, JobTriggerType.RUNTIME_READY)
            hardware = TrackingHardware()
            backend = TrackingBackend()
            platform = FailingOperationalPlatform()
            app = self.app(
                store, backend, hardware=hardware, platform_provider=platform,
            )

            with self.assertRaisesRegex(RuntimeError, "operational sample failed"):
                await app.start()

            self.assertEqual(platform.calls, 2)
            self.assertEqual(hardware.start_calls, 1)
            self.assertEqual(backend.prepare_calls, 1)
            self.assertFalse(app._runtime_ready_observed)
            self.assertIs(app.state, LifecycleState.STOPPED)
            self.assertEqual(hardware.stop_calls, 1)
            self.assertFalse(hardware.is_running)
            await app.stop()  # Cleanup remains safely idempotent after failed startup.
            reopened = SQLiteJobStore(path)
            self.assertEqual(reopened.list_runs(job.id), ())
            reopened.close()

    async def test_busy_attention_defers_without_starting_parallel_cognition(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteJobStore(Path(temporary) / "jobs.sqlite3")
            jobs = [store.create_job(name) for name in ("Capabilities", "Embodiment")]
            for job in jobs:
                store.set_trigger(job.id, JobTriggerType.RUNTIME_READY)
            backend = BlockingBackend()
            app = self.app(store, backend, capacity=2)
            app.episode_coordinator._operator_waiters = 1
            await app.start()
            await self.drain()
            self.assertTrue(all(store.list_runs(job.id) == () for job in jobs))
            self.assertFalse(backend.started.is_set())
            pending = {(job.id, JobTriggerType.RUNTIME_READY) for job in jobs}
            self.assertEqual(app._pending_job_triggers, pending)
            await app._activate_triggered_jobs(JobTriggerType.RUNTIME_READY)
            self.assertEqual(app._pending_job_triggers, pending)
            app.episode_coordinator._operator_waiters = 0
            await app._offer_job_activations()
            await backend.started.wait()
            await self.drain()
            self.assertTrue(all(len(store.list_runs(job.id)) == 1 for job in jobs))
            self.assertEqual(app._pending_job_triggers, set())
            await app.stop()

    async def test_one_runtime_ready_occurrence_fans_out_to_two_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteJobStore(Path(temporary) / "jobs.sqlite3")
            jobs = [store.create_job(name) for name in ("Capabilities", "Embodiment")]
            for job in jobs:
                store.set_trigger(job.id, JobTriggerType.RUNTIME_READY)
            app = self.app(store, BlockingBackend(), capacity=2)

            await app.start()
            await self.drain()

            runs = [store.list_runs(job.id) for job in jobs]
            self.assertTrue(all(len(items) == 1 for items in runs))
            self.assertEqual(len({items[0].id for items in runs}), 2)
            self.assertEqual(len(app.job_execution_contexts), 2)
            self.assertEqual(app.job_work_slots_occupied, 2)
            self.assertEqual(len(app.episode_coordinator.current_autonomous_episodes), 2)
            await app.stop()

    async def test_capacity_shortage_coalesces_pending_per_subscription(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteJobStore(Path(temporary) / "jobs.sqlite3")
            jobs = [store.create_job(name) for name in ("First", "Second")]
            for job in jobs:
                store.set_trigger(job.id, JobTriggerType.RUNTIME_READY)
            app = self.app(store, BlockingBackend(), capacity=1)
            await app.start()
            await self.drain()

            self.assertEqual(len(store.list_runs(jobs[0].id)), 1)
            self.assertEqual(store.list_runs(jobs[1].id), ())
            pending = {(jobs[1].id, JobTriggerType.RUNTIME_READY)}
            self.assertEqual(app._pending_job_triggers, pending)
            await app._activate_triggered_jobs(JobTriggerType.RUNTIME_READY)
            self.assertEqual(app._pending_job_triggers, pending)
            first_run = store.list_runs(jobs[0].id)[0]
            await app.finish_job_run_by_id(first_run.id, JobRunStatus.STOPPED)
            await app._offer_job_activations()
            await self.drain()
            self.assertEqual(len(store.list_runs(jobs[1].id)), 1)
            await app.stop()

    async def test_disabled_pending_subscriber_is_discarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteJobStore(Path(temporary) / "jobs.sqlite3")
            jobs = [store.create_job(name) for name in ("First", "Second")]
            for job in jobs:
                store.set_trigger(job.id, JobTriggerType.RUNTIME_READY)
            app = self.app(store, BlockingBackend(), capacity=1)
            await app.start()
            await self.drain()
            store.set_job_enabled(jobs[1].id, False)
            await app._offer_job_activations()
            self.assertNotIn((jobs[1].id, JobTriggerType.RUNTIME_READY),
                             app._pending_job_triggers)
            self.assertEqual(store.list_runs(jobs[1].id), ())
            await app.stop()

    async def test_console_configuration_immediately_discards_pending_intents(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteJobStore(Path(temporary) / "jobs.sqlite3")
            jobs = [store.create_job(name) for name in ("Untrigger", "Disable")]
            for job in jobs:
                store.set_trigger(job.id, JobTriggerType.RUNTIME_READY)
            app = self.app(store, BlockingBackend(), capacity=2)
            app.episode_coordinator._operator_waiters = 1
            await app.start()
            await self.drain()
            console = RuntimeConsole(app)
            self.assertEqual(len(app._pending_job_triggers), 2)

            report, _ = console.execute(
                f"job untrigger JOB{jobs[0].id} runtime_ready")
            self.assertIn("Removed", report)
            self.assertNotIn((jobs[0].id, JobTriggerType.RUNTIME_READY),
                             app._pending_job_triggers)
            self.assertIn((jobs[1].id, JobTriggerType.RUNTIME_READY),
                          app._pending_job_triggers)

            report, _ = console.execute(f"job disable JOB{jobs[1].id}")
            self.assertIn("disabled", report)
            self.assertEqual(app._pending_job_triggers, set())
            self.assertTrue(all(store.list_runs(job.id) == () for job in jobs))
            await app.stop()

    async def test_one_subscriber_persistence_failure_does_not_block_another(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteJobStore(Path(temporary) / "jobs.sqlite3")
            jobs = [store.create_job(name) for name in ("Broken", "Healthy")]
            app = self.app(store, BlockingBackend(), capacity=2)
            await app.start()
            for job in jobs:
                store.set_trigger(job.id, JobTriggerType.RUNTIME_READY)
            original = store.create_triggered_run

            def create(job_id):
                if job_id == jobs[0].id:
                    raise RuntimeError("injected persistence failure")
                return original(job_id)

            with patch.object(store, "create_triggered_run", side_effect=create):
                await app._activate_triggered_jobs(JobTriggerType.RUNTIME_READY)
            await self.drain()
            self.assertEqual(store.list_runs(jobs[0].id), ())
            self.assertEqual(len(store.list_runs(jobs[1].id)), 1)
            await app.stop()
