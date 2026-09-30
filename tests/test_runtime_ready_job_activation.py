import asyncio
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import ApplicationOptions, LifecycleState, RobotApplication
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
    def app(self, store, backend, *, hardware=None, platform_provider=None):
        async def sleep_forever(_delay):
            await asyncio.Event().wait()

        return RobotApplication(
            RobotProfile("test", "Test"), hardware or VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True),
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
            job = store.create_job("Inspect capabilities")
            store.set_trigger(job.id, JobTriggerType.RUNTIME_READY)
            backend = BlockingBackend()
            app = self.app(store, backend)
            app.episode_coordinator._operator_waiters = 1
            await app.start()
            await self.drain()
            self.assertEqual(store.list_runs(job.id), ())
            self.assertFalse(backend.started.is_set())
            self.assertEqual(app._pending_job_triggers, {JobTriggerType.RUNTIME_READY})
            app.episode_coordinator._operator_waiters = 0
            await app._offer_job_activations()
            await backend.started.wait()
            self.assertEqual(len(store.list_runs(job.id)), 1)
            await app.stop()
