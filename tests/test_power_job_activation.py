import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.events import PowerAttentionRequired, PowerRecovered
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import JobRunStatus, JobTriggerType, SQLiteJobStore
from embodied_runtime.profile import RobotProfile
from embodied_runtime.state import PowerCondition, PowerState
from tests.test_job_execution import BlockingBackend, JobBackend, Platform
from tests.test_job_readiness import ReadinessBackend


class BatteryHardware(VirtualHardwareBackend):
    capabilities = ("battery_voltage",)

    def __init__(self, voltage):
        super().__init__()
        self.voltage = voltage

    def read_battery_voltage_v(self):
        return self.voltage


class PowerJobActivationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.temp.cleanup()

    def app(self, backend, hardware=None, *, auto_continue=False):
        async def sleep_forever(_delay):
            await asyncio.Event().wait()
        return RobotApplication(
            RobotProfile("test", "Test"), hardware or VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True,
                initiative_goal_closure_enabled=True, jobs_auto_continue=auto_continue),
            platform_provider=Platform(), cognition_backend=backend, job_store=self.store,
            wall_clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
            job_scheduler_sleep=sleep_forever, job_continuation_sleep=sleep_forever,
            power_monitor_sleep=sleep_forever)

    async def drain(self):
        for _ in range(20):
            await asyncio.sleep(0)

    def configured_job(self, *, enabled=True):
        job = self.store.create_job("Tend power", enabled=enabled)
        self.store.set_trigger(job.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
        return job

    async def test_trigger_starts_real_bounded_work_and_duplicate_coalesces(self):
        backend = BlockingBackend()
        job = self.configured_job()
        app = self.app(backend)
        await app.start()
        await app.events.publish(PowerAttentionRequired(
            source="test", battery_voltage_v=7.3))
        await backend.started.wait()
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.assertIsNotNone(app.current_task)
        self.assertIsNotNone(app.active_goal)
        await app.events.publish(PowerAttentionRequired(
            source="test", battery_voltage_v=7.2))
        await self.drain()
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        await app.stop()

    async def test_busy_activation_is_retained_then_admitted_without_new_event(self):
        backend = JobBackend("completed")
        job = self.configured_job()
        app = self.app(backend)
        app.episode_coordinator._operator_waiters = 1
        await app.start()
        app._replace_power_state(PowerState(
            7.3, PowerCondition.ATTENTION, datetime(2026, 9, 20, tzinfo=UTC)))
        await app.events.publish(PowerAttentionRequired(
            source="test", battery_voltage_v=7.3))
        await self.drain()
        self.assertEqual(self.store.list_runs(job.id), ())
        self.assertEqual(app._pending_job_triggers,
                         {(job.id, JobTriggerType.POWER_ATTENTION_REQUIRED)})
        app.episode_coordinator._operator_waiters = 0
        await app._offer_job_activations()
        await app._active_job_work_task
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.assertEqual(app._pending_job_triggers, set())
        await app.stop()

    async def test_recovery_discards_busy_pending_activation(self):
        backend = JobBackend("completed")
        job = self.configured_job()
        app = self.app(backend)
        app.episode_coordinator._operator_waiters = 1
        await app.start()
        app._replace_power_state(PowerState(
            7.3, PowerCondition.ATTENTION, datetime(2026, 9, 20, tzinfo=UTC)))
        await app.events.publish(PowerAttentionRequired(
            source="test", battery_voltage_v=7.3))
        await self.drain()
        self.assertEqual(app._pending_job_triggers,
                         {(job.id, JobTriggerType.POWER_ATTENTION_REQUIRED)})
        self.assertEqual(self.store.list_runs(job.id), ())

        app._replace_power_state(PowerState(
            7.8, PowerCondition.NORMAL, datetime(2026, 9, 20, tzinfo=UTC)))
        await app.events.publish(PowerRecovered(source="test", battery_voltage_v=7.8))
        await self.drain()
        app.episode_coordinator._operator_waiters = 0
        await app._offer_job_activations()

        self.assertEqual(app._pending_job_triggers, set())
        self.assertEqual(self.store.list_runs(job.id), ())
        self.assertEqual(backend.requests, [])
        await app.stop()

    async def test_disabled_or_removed_authority_drops_pending_activation(self):
        job = self.configured_job(enabled=False)
        app = self.app(JobBackend("completed"))
        await app.start()
        await app.events.publish(PowerAttentionRequired(
            source="test", battery_voltage_v=7.3))
        await self.drain()
        self.assertEqual(self.store.list_runs(job.id), ())
        self.store.set_job_enabled(job.id, True)
        self.store.remove_trigger(job.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
        await app._offer_job_activations()
        self.assertEqual(self.store.list_runs(job.id), ())
        await app.stop()

    async def test_power_recovery_wakes_same_exact_run_with_bounded_evidence(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "charging",
             "readiness": "wait_for_event", "delay_seconds": None,
             "event_type": "power_recovered"},
            {"disposition": "completed", "summary": "recovered",
             "readiness": None, "delay_seconds": None, "event_type": None},
        ))
        job = self.store.create_job("Power")
        app = self.app(backend, auto_continue=True)
        await app.start()
        binding = app.start_job_run(job.id)
        await app.work_current_job_once()
        await app.events.publish(PowerRecovered(source="test", battery_voltage_v=7.8))
        await self.drain()
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.assertEqual(self.store.list_runs(job.id)[0].id, binding.run.id)
        self.assertIs(self.store.list_runs(job.id)[0].status, JobRunStatus.COMPLETED)
        self.assertTrue(any("continuation_wake_event: power_recovered" in (request[1] or "")
                            for request in backend.requests))
        await app.stop()

    async def test_power_recovery_with_stale_task_binding_fails_closed(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "charging",
             "readiness": "wait_for_event", "delay_seconds": None,
             "event_type": "power_recovered"},
        ))
        job = self.store.create_job("Power")
        app = self.app(backend, auto_continue=True)
        await app.start()
        app.start_job_run(job.id)
        await app.work_current_job_once()
        app._job_continuation = replace(app.job_continuation, task_id=uuid4())
        request_count = len(backend.requests)
        await app.events.publish(PowerRecovered(source="test", battery_voltage_v=7.8))
        await self.drain()
        self.assertEqual(len(backend.requests), request_count)
        self.assertFalse(app.job_continuation.event_satisfied)
        await app.stop()

    async def test_startup_attention_creates_new_run_not_interrupted_history(self):
        job = self.configured_job()
        old = self.store.create_run(job.id)
        self.store.transition_run(old.id, JobRunStatus.INTERRUPTED)
        app = self.app(JobBackend("completed"), BatteryHardware(7.3))
        await app.start()
        await self.drain()
        if app._active_job_work_task is not None:
            await app._active_job_work_task
        runs = self.store.list_runs(job.id)
        self.assertEqual(len(runs), 2)
        self.assertIs(runs[0].status, JobRunStatus.INTERRUPTED)
        # Current power authority still reports ATTENTION, so successful completion
        # is rejected and the occurrence remains available for corrected work.
        self.assertIs(runs[1].status, JobRunStatus.RUNNING)
        await app.stop()
