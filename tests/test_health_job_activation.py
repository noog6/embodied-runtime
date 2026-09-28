import asyncio
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.events import (
    MemoryPressureCleared, MemoryPressureRaised,
    ThermalWarningCleared, ThermalWarningRaised,
)
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import JobRunStatus, JobTriggerType, SQLiteJobStore
from embodied_runtime.profile import RobotProfile
from tests.test_platform import snapshot
from tests.test_job_execution import BlockingBackend, Platform
from tests.test_job_readiness import ReadinessBackend


class SequencePlatform:
    def __init__(self, snapshots):
        self._snapshots = iter(snapshots)

    def snapshot(self):
        value = next(self._snapshots)
        if isinstance(value, Exception):
            raise value
        return value


class HealthJobActivationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")
        self._startup_case = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def app(self, backend, *, auto_continue=False, platform_provider=None):
        async def sleep_forever(_delay):
            await asyncio.Event().wait()
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True,
                initiative_goal_closure_enabled=True, jobs_auto_continue=auto_continue),
            platform_provider=platform_provider or Platform(), cognition_backend=backend,
            job_store=self.store,
            wall_clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
            job_scheduler_sleep=sleep_forever, job_continuation_sleep=sleep_forever,
        )

    async def drain(self):
        for _ in range(20):
            await asyncio.sleep(0)

    async def test_deferred_thermal_activation_revalidates_current_condition(self):
        await self._assert_deferred_activation_revalidates(
            JobTriggerType.THERMAL_WARNING_RAISED,
            snapshot(cpu_temperature_celsius=82, memory_total_bytes=1000,
                     memory_available_bytes=500),
            snapshot(cpu_temperature_celsius=74, memory_total_bytes=1000,
                     memory_available_bytes=500),
        )

    async def test_deferred_memory_activation_revalidates_current_condition(self):
        await self._assert_deferred_activation_revalidates(
            JobTriggerType.MEMORY_PRESSURE_RAISED,
            snapshot(cpu_temperature_celsius=70, memory_total_bytes=1000,
                     memory_available_bytes=90),
            snapshot(cpu_temperature_celsius=70, memory_total_bytes=1000,
                     memory_available_bytes=200),
        )

    async def _assert_deferred_activation_revalidates(self, trigger_type, raised, cleared):
        normal = snapshot(cpu_temperature_celsius=70, memory_total_bytes=1000,
                          memory_available_bytes=500)
        provider = SequencePlatform([normal, normal, raised, cleared])
        job = self.store.create_job("Deferred health")
        self.store.set_trigger(job.id, trigger_type)
        app = self.app(ReadinessBackend(()), platform_provider=provider)
        app.episode_coordinator._operator_waiters = 1
        await app.start()
        await app._platform_monitor.sample_platform_once()
        await self.drain()
        self.assertEqual(app._pending_job_triggers, {trigger_type})
        await app._platform_monitor.sample_platform_once()
        # Retry before queued clear-event delivery: monitor hysteresis is authoritative.
        app.episode_coordinator._operator_waiters = 0
        await app._offer_job_activations()
        self.assertEqual(self.store.list_runs(job.id), ())
        self.assertEqual(app._pending_job_triggers, set())
        await app.stop()

    async def test_startup_health_reconciliation(self):
        normal = snapshot(cpu_temperature_celsius=70, memory_total_bytes=1000,
                          memory_available_bytes=500)
        hot = snapshot(cpu_temperature_celsius=82, memory_total_bytes=1000,
                       memory_available_bytes=500)
        pressured = snapshot(cpu_temperature_celsius=70, memory_total_bytes=1000,
                             memory_available_bytes=90)
        both = snapshot(cpu_temperature_celsius=82, memory_total_bytes=1000,
                        memory_available_bytes=90)
        cases = (
            ("early_hot_current_normal", hot, normal,
             (JobTriggerType.THERMAL_WARNING_RAISED,), 0),
            ("early_normal_current_hot", normal, hot,
             (JobTriggerType.THERMAL_WARNING_RAISED,), 1),
            ("early_pressure_current_normal", pressured, normal,
             (JobTriggerType.MEMORY_PRESSURE_RAISED,), 0),
            ("early_normal_current_pressure", normal, pressured,
             (JobTriggerType.MEMORY_PRESSURE_RAISED,), 1),
            ("normal", normal, normal, (), 0),
            ("both", normal, both,
             (JobTriggerType.THERMAL_WARNING_RAISED,
              JobTriggerType.MEMORY_PRESSURE_RAISED), 1),
        )
        for name, early, operational, triggers, expected_runs in cases:
            with self.subTest(name=name):
                await self._assert_startup_reconciliation(
                    early, operational, triggers, expected_runs)

    async def test_failed_operational_sample_does_not_publish_stale_health(self):
        early_hot = snapshot(cpu_temperature_celsius=82, memory_total_bytes=1000,
                             memory_available_bytes=500)
        job = self.store.create_job("Startup health")
        self.store.set_trigger(job.id, JobTriggerType.THERMAL_WARNING_RAISED)
        app = self.app(
            ReadinessBackend(()),
            platform_provider=SequencePlatform([early_hot, RuntimeError("sample failed")]),
        )
        with self.assertLogs("embodied_runtime.app", level="ERROR"), \
                self.assertRaisesRegex(RuntimeError, "sample failed"):
            await app.start()
        self.assertEqual(app.state.value, "stopped")
        reopened = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")
        self.assertEqual(reopened.list_runs(job.id), ())
        reopened.close()

    async def _assert_startup_reconciliation(
        self, early, operational, triggers, expected_runs,
    ):
        # Each subtest needs an independent store because application shutdown owns it.
        self._startup_case += 1
        path = Path(self.temp.name) / f"startup-{self._startup_case}.db"
        store = SQLiteJobStore(path)
        job = store.create_job("Startup health")
        for trigger in triggers:
            store.set_trigger(job.id, trigger)
        original, self.store = self.store, store
        app = self.app(
            BlockingBackend(),
            platform_provider=SequencePlatform([early, operational, operational]),
        )
        try:
            await app.start()
            await self.drain()
            self.assertIs(app.runtime_state.platform, operational)
            self.assertEqual(len(store.list_runs(job.id)), expected_runs)
            self.assertEqual(app._pending_job_triggers, set())
            await app._platform_monitor.sample_platform_once()
            await self.drain()
            self.assertEqual(len(store.list_runs(job.id)), expected_runs)
            await app.stop()
        finally:
            self.store = original

    async def test_thermal_raise_activates_once_and_repeats_coalesce(self):
        await self._assert_raise_coalesces(
            JobTriggerType.THERMAL_WARNING_RAISED,
            lambda: ThermalWarningRaised(source="test", cpu_temperature_celsius=82.0,
                                          warning_threshold_celsius=80.0))

    async def test_memory_raise_activates_once_and_repeats_coalesce(self):
        await self._assert_raise_coalesces(
            JobTriggerType.MEMORY_PRESSURE_RAISED,
            lambda: MemoryPressureRaised(source="test", memory_available_bytes=90,
                                          memory_total_bytes=1000, available_ratio=.09,
                                          pressure_threshold_ratio=.10))

    async def _assert_raise_coalesces(self, trigger_type, event):
        backend = BlockingBackend()
        job = self.store.create_job(f"Health {trigger_type.value}")
        self.store.set_trigger(job.id, trigger_type)
        app = self.app(backend)
        await app.start()
        await app.events.publish(event())
        await backend.started.wait()
        await app.events.publish(event())
        await self.drain()
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        await app.stop()

    async def test_thermal_recovery_wakes_exact_run_with_bounded_evidence(self):
        await self._assert_recovery(
            JobTriggerType.THERMAL_WARNING_RAISED,
            ThermalWarningRaised(source="test", cpu_temperature_celsius=82.0,
                                 warning_threshold_celsius=80.0),
            lambda: ThermalWarningCleared(source="test", cpu_temperature_celsius=74.0,
                                          clear_threshold_celsius=75.0),
            "thermal_warning_cleared",
            ("cpu_temperature_celsius_at_transition: 74.0", "threshold_celsius: 75.0"),
        )

    async def test_memory_recovery_wakes_exact_run_with_bounded_evidence(self):
        await self._assert_recovery(
            JobTriggerType.MEMORY_PRESSURE_RAISED,
            MemoryPressureRaised(source="test", memory_available_bytes=90,
                                 memory_total_bytes=1000, available_ratio=.09,
                                 pressure_threshold_ratio=.10),
            lambda: MemoryPressureCleared(source="test", memory_available_bytes=200,
                                          memory_total_bytes=1000, available_ratio=.20,
                                          clear_threshold_ratio=.15),
            "memory_pressure_cleared",
            ("memory_available_bytes_at_transition: 200", "memory_total_bytes_at_transition: 1000",
             "available_ratio_at_transition: 0.200000", "threshold_ratio: 0.150000"),
        )

    async def _assert_recovery(self, trigger_type, raised, cleared, event_name, facts):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "wait", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": event_name},
            {"disposition": "completed", "summary": "reassessed", "readiness": None,
             "delay_seconds": None, "event_type": None},
        ))
        job = self.store.create_job(f"Recover {event_name}")
        self.store.set_trigger(job.id, trigger_type)
        app = self.app(backend, auto_continue=True)
        await app.start()
        await app.events.publish(raised)
        await self.drain()
        run_id = self.store.list_runs(job.id)[0].id
        await app.events.publish(cleared())
        await self.drain()
        runs = self.store.list_runs(job.id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].id, run_id)
        self.assertIs(runs[0].status, JobRunStatus.COMPLETED)
        instructions = "\n".join(request[1] or "" for request in backend.requests)
        self.assertIn(f"continuation_wake_event: {event_name}", instructions)
        for fact in facts:
            self.assertIn(fact, instructions)
        await app.stop()

    async def test_second_trigger_coalesces_while_current_state_retains_pressure(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "hot",
             "readiness": "wait_for_event", "delay_seconds": None,
             "event_type": "thermal_warning_cleared"},
            {"disposition": "completed", "summary": "reassessed",
             "readiness": None, "delay_seconds": None, "event_type": None},
        ))
        job = self.store.create_job("Tend runtime health")
        self.store.set_trigger(job.id, JobTriggerType.THERMAL_WARNING_RAISED)
        self.store.set_trigger(job.id, JobTriggerType.MEMORY_PRESSURE_RAISED)
        normal = snapshot(cpu_temperature_celsius=70,
                          memory_total_bytes=1000 * 1024 * 1024,
                          memory_available_bytes=500 * 1024 * 1024)
        provider = SequencePlatform([
            normal,
            normal,
            snapshot(cpu_temperature_celsius=82, memory_total_bytes=1000 * 1024 * 1024,
                     memory_available_bytes=500 * 1024 * 1024),
            snapshot(cpu_temperature_celsius=82, memory_total_bytes=1000 * 1024 * 1024,
                     memory_available_bytes=90 * 1024 * 1024),
            snapshot(cpu_temperature_celsius=74, memory_total_bytes=1000 * 1024 * 1024,
                     memory_available_bytes=90 * 1024 * 1024),
        ])
        app = self.app(backend, auto_continue=True, platform_provider=provider)
        await app.start()
        await app._platform_monitor.sample_platform_once()
        await self.drain()
        first = self.store.list_runs(job.id)[0]
        await app._platform_monitor.sample_platform_once()
        await self.drain()
        self.assertEqual(self.store.list_runs(job.id), (first,))
        self.assertEqual(app.runtime_state.platform.memory_available_bytes, 90 * 1024 * 1024)
        await app._platform_monitor.sample_platform_once()
        await self.drain()
        self.assertEqual(app.runtime_state.platform.memory_available_bytes, 90 * 1024 * 1024)
        instructions = "\n".join(request[1] or "" for request in backend.requests)
        self.assertIn("continuation_wake_event: thermal_warning_cleared", instructions)
        self.assertIn("memory_available_mib: 90.0", instructions)
        self.assertNotIn("memory_pressure_cleared", instructions)
        await app.stop()

    async def test_health_recovery_is_sticky_while_unrelated_attention_is_busy(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "hot",
             "readiness": "wait_for_event", "delay_seconds": None,
             "event_type": "thermal_warning_cleared"},
            {"disposition": "completed", "summary": "reassessed",
             "readiness": None, "delay_seconds": None, "event_type": None},
        ))
        job = self.store.create_job("Tend runtime health")
        app = self.app(backend, auto_continue=True)
        await app.start()
        binding = app.start_job_run(job.id)
        await app.work_current_job_once()
        busy = app.episode_coordinator.try_start("test", "test", "unrelated", None)
        await app.events.publish(ThermalWarningCleared(
            source="test", cpu_temperature_celsius=74,
            clear_threshold_celsius=75))
        await self.drain()
        self.assertTrue(app.job_continuation.event_satisfied)
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        app.episode_coordinator.close(busy, "handled")
        app._offer_job_continuation()
        await self.drain()
        runs = self.store.list_runs(job.id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].id, binding.run.id)
        self.assertIs(runs[0].status, JobRunStatus.COMPLETED)
        await app.stop()
