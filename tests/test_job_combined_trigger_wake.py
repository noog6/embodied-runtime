import asyncio
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.events import (
    MemoryPressureRaised, PowerAttentionRequired, ThermalWarningRaised,
)
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    JobContinuation, JobContinuationReadiness, JobContinuationState,
    JobReadinessEventType, JobRunStatus, JobTriggerType, SQLiteJobStore,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.state import PowerCondition, PowerState
from tests.test_job_execution import BlockingBackend, Platform


class CombinedTriggerWakeTests(unittest.IsolatedAsyncioTestCase):
    async def _drain(self):
        for _ in range(20):
            await asyncio.sleep(0)

    def _app(self, store, backend, *, capacity=2):
        async def sleep_forever(_delay):
            await asyncio.Event().wait()

        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True, jobs_auto_continue=True,
                               jobs_max_concurrent_work=capacity),
            platform_provider=Platform(), cognition_backend=backend,
            job_store=store, wall_clock=lambda: datetime(2026, 10, 2, tzinfo=UTC),
            job_scheduler_sleep=sleep_forever,
            job_continuation_sleep=sleep_forever,
            power_monitor_sleep=sleep_forever,
        )

    @staticmethod
    def _waiting(context, event_type):
        context.continuation = JobContinuation(
            context.job_id, context.run_id, context.task_id,
            JobContinuationState.ARMED, 3, "waiting",
            JobContinuationReadiness.WAIT_FOR_EVENT,
            event_type=event_type, event_armed_after_ns=0,
        )

    async def test_each_raised_event_wakes_existing_run_and_fans_out(self):
        cases = (
            (JobTriggerType.POWER_ATTENTION_REQUIRED,
             JobReadinessEventType.POWER_ATTENTION_REQUIRED,
             PowerAttentionRequired(source="test", battery_voltage_v=7.3)),
            (JobTriggerType.THERMAL_WARNING_RAISED,
             JobReadinessEventType.THERMAL_WARNING_RAISED,
             ThermalWarningRaised(source="test", cpu_temperature_celsius=82.0,
                                  warning_threshold_celsius=80.0)),
            (JobTriggerType.MEMORY_PRESSURE_RAISED,
             JobReadinessEventType.MEMORY_PRESSURE_RAISED,
             MemoryPressureRaised(source="test", memory_available_bytes=90,
                                  memory_total_bytes=1000, available_ratio=.09,
                                  pressure_threshold_ratio=.10)),
        )
        for trigger_type, readiness_type, event in cases:
            with self.subTest(trigger=trigger_type), tempfile.TemporaryDirectory() as temp:
                store = SQLiteJobStore(Path(temp) / "jobs.sqlite3")
                jobs = [store.create_job(name) for name in ("Existing", "Subscriber")]
                for job in jobs:
                    store.set_trigger(job.id, trigger_type)
                app = self._app(store, BlockingBackend())
                await app.start()
                existing = app.start_job_run(jobs[0].id)
                context_a = app._context_for_run(existing.run.id)
                self._waiting(context_a, readiness_type)

                await app.events.publish(event)
                await self._drain()

                self.assertEqual(context_a.continuation.automatic_steps_remaining, 2)
                self.assertEqual(len(store.list_runs(jobs[0].id)), 1)
                runs_b = store.list_runs(jobs[1].id)
                self.assertEqual(len(runs_b), 1)
                context_b = app._context_for_run(runs_b[0].id)
                self.assertIsNot(context_a, context_b)
                self.assertEqual((context_a.job_id, context_b.job_id),
                                 (jobs[0].id, jobs[1].id))
                self.assertIsNotNone(context_a.active_work_task)
                self.assertIsNotNone(context_b.active_work_task)
                self.assertEqual(app.job_work_slots_occupied, 2)
                self.assertEqual(app._pending_job_triggers, set())
                await app.stop()

    async def test_combined_wake_retains_one_pending_intent_at_capacity(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteJobStore(Path(temp) / "jobs.sqlite3")
            jobs = [store.create_job(name) for name in ("Waiting", "Subscriber", "Busy")]
            for job in jobs[:2]:
                store.set_trigger(job.id, JobTriggerType.POWER_ATTENTION_REQUIRED)
            backend = BlockingBackend()
            app = self._app(store, backend, capacity=1)
            await app.start()
            waiting = app.start_job_run(jobs[0].id)
            context_a = app._context_for_run(waiting.run.id)
            self._waiting(context_a, JobReadinessEventType.POWER_ATTENTION_REQUIRED)
            busy = app.start_job_run(jobs[2].id)
            busy_work = asyncio.create_task(app.work_job_run_once(busy.run.id))
            await backend.started.wait()
            app._replace_power_state(PowerState(
                7.3, PowerCondition.ATTENTION, datetime(2026, 10, 2, tzinfo=UTC)))
            event = PowerAttentionRequired(source="test", battery_voltage_v=7.3)

            await app.events.publish(event)
            await app.events.publish(event)
            await self._drain()

            pending = {(jobs[1].id, JobTriggerType.POWER_ATTENTION_REQUIRED)}
            self.assertTrue(context_a.continuation.event_satisfied)
            self.assertEqual(len(store.list_runs(jobs[0].id)), 1)
            self.assertEqual(store.list_runs(jobs[1].id), ())
            self.assertEqual(app._pending_job_triggers, pending)
            await app.finish_job_run_by_id(busy.run.id, JobRunStatus.STOPPED)
            self.assertTrue(busy_work.cancelled())
            await app._offer_job_activations()
            await self._drain()
            self.assertEqual(len(store.list_runs(jobs[1].id)), 1)
            self.assertEqual(app._pending_job_triggers, set())
            await app.stop()
