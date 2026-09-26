import asyncio
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.console import RuntimeConsole
from embodied_runtime.events import BodyOrientationChanged, PresenceChanged
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    JobContinuationReadiness, JobContinuationState, JobReadinessEventType,
    JobRunStatus, SQLiteJobStore,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.tasks import TaskStatus
from tests.test_job_execution import Platform
from tests.test_job_readiness import ReadinessBackend


class JobEventReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def app(self, backend, *, max_steps=3):
        async def sleep_forever(_delay):
            await asyncio.Event().wait()
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True,
                               initiative_goal_closure_enabled=True,
                               jobs_auto_continue=True, jobs_max_auto_steps=max_steps),
            platform_provider=Platform(), cognition_backend=backend,
            job_store=self.store,
            wall_clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
            job_continuation_sleep=sleep_forever,
        )

    async def start_waiting(self, proposals, *, max_steps=3):
        backend = ReadinessBackend(proposals)
        app = self.app(backend, max_steps=max_steps)
        await app.start()
        app.start_job_run(self.store.create_job("Wait for presence").id)
        await app.work_current_job_once()
        return app, backend

    async def drain(self):
        for _ in range(15):
            await asyncio.sleep(0)

    async def publish_presence(self, app, present=True):
        await app.events.publish(PresenceChanged(
            source="test", previous_present=not present, present=present,
        ))
        await self.drain()

    async def test_matching_event_immediately_offers_one_episode(self):
        app, backend = await self.start_waiting((
            {"disposition": "continue", "summary": "A", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "B", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
        ))
        continuation = app.job_continuation
        self.assertIs(continuation.event_type, JobReadinessEventType.PRESENCE_CHANGED)
        for _ in range(3):
            app._offer_job_continuation()
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(continuation.automatic_steps_remaining, 3)

        await self.publish_presence(app)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        self.assertFalse(app.job_continuation.event_satisfied)
        self.assertIsNone(app.job_continuation.wake_event)
        self.assertEqual(len(backend.requests), 4)
        self.assertIn("Previous work summary", backend.requests[2][1])
        self.assertIn("continuation_wake_event: presence_changed", backend.requests[2][1])
        # The new wait installed by episode two needs a new event.
        app._offer_job_continuation()
        await self.drain()
        self.assertEqual(len(backend.requests), 4)
        await self.publish_presence(app, False)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 1)
        await app.stop()

    async def test_repeated_event_and_heartbeat_offers_are_single_flight(self):
        app, backend = await self.start_waiting((
            {"disposition": "continue", "summary": "A", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "B", "readiness": "ready",
             "delay_seconds": None, "event_type": None},
        ))
        busy = app.episode_coordinator.try_start("test", "test", "busy", None)
        await self.publish_presence(app)
        wake = app.job_continuation.wake_event
        await self.publish_presence(app, False)
        self.assertIs(app.job_continuation.wake_event, wake)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        app.episode_coordinator.close(busy, "handled")
        app._offer_job_continuation()
        app._offer_job_continuation()
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        await self.drain()
        self.assertEqual(len(backend.requests), 4)
        await app.stop()

    async def test_event_during_active_episode_does_not_cross_new_arm(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingBackend(ReadinessBackend):
            async def respond(self, message, **kwargs):
                if len(self.requests) == 2:
                    started.set()
                    await release.wait()
                return await super().respond(message, **kwargs)

        backend = BlockingBackend((
            {"disposition": "continue", "summary": "A", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "B", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "C", "readiness": "ready",
             "delay_seconds": None, "event_type": None},
        ))
        app = self.app(backend)
        await app.start()
        app.start_job_run(self.store.create_job("Boundary").id)
        await app.work_current_job_once()
        await self.publish_presence(app)
        await started.wait()
        active = app._active_job_work_task
        await self.publish_presence(app, False)
        self.assertIs(app._active_job_work_task, active)
        release.set()
        await active
        self.assertFalse(app.job_continuation.event_satisfied)
        self.assertIsNone(app.job_continuation.wake_event)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        app._offer_job_continuation()
        await self.drain()
        self.assertEqual(len(backend.requests), 4)
        await self.publish_presence(app)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 1)
        self.assertEqual(len(backend.requests), 6)
        await app.stop()

    async def test_operator_waiter_cannot_consume_or_outrank_wake(self):
        app, backend = await self.start_waiting((
            {"disposition": "continue", "summary": "A", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "B", "readiness": "ready",
             "delay_seconds": None, "event_type": None},
        ))
        app.episode_coordinator._operator_waiters = 1
        await self.publish_presence(app)
        wake = app.job_continuation.wake_event
        self.assertTrue(app.job_continuation.event_satisfied)
        self.assertIsNotNone(wake)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        self.assertIsNone(app._active_job_work_task)
        self.assertEqual(len(backend.requests), 2)
        app._offer_job_continuation()
        self.assertTrue(app.job_continuation.event_satisfied)
        self.assertIs(app.job_continuation.wake_event, wake)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        app.episode_coordinator._operator_waiters = 0
        app._offer_job_continuation()
        self.assertFalse(app.job_continuation.event_satisfied)
        self.assertIsNone(app.job_continuation.wake_event)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        await self.drain()
        self.assertIn("Previous work summary", backend.requests[2][1])
        self.assertIn("continuation_wake_event: presence_changed", backend.requests[2][1])
        await app.stop()

    async def test_busy_attention_cannot_consume_wake(self):
        app, backend = await self.start_waiting((
            {"disposition": "continue", "summary": "A", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "B", "readiness": "ready",
             "delay_seconds": None, "event_type": None},
        ))
        busy = app.episode_coordinator.try_start("test", "test", "busy", None)
        self.assertIsNotNone(busy)
        await self.publish_presence(app)
        wake = app.job_continuation.wake_event
        app._offer_job_continuation()
        self.assertTrue(app.job_continuation.event_satisfied)
        self.assertIs(app.job_continuation.wake_event, wake)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        self.assertEqual(len(backend.requests), 2)
        app.episode_coordinator.close(busy, "handled")
        app._offer_job_continuation()
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        await self.drain()
        self.assertIn("continuation_wake_event: presence_changed", backend.requests[2][1])
        await app.stop()

    async def test_event_selector_combinations_are_fail_closed(self):
        invalid = (
            {"disposition": "completed", "summary": "x", "readiness": None,
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "failed", "summary": "x", "readiness": None,
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "x", "readiness": "ready",
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "x", "readiness": "after_delay",
             "delay_seconds": 1, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "x", "readiness": "wait_for_operator",
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "x", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": None},
            {"disposition": "continue", "summary": "x", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "unknown"},
            {"disposition": "continue", "summary": "x", "readiness": "wait_for_event",
             "delay_seconds": 1, "event_type": "presence_changed"},
        )
        backend = ReadinessBackend(invalid)
        app = self.app(backend)
        await app.start()
        app.start_job_run(self.store.create_job("Validate").id)
        for proposal in invalid:
            with self.subTest(proposal=proposal):
                outcome = await app.work_current_job_once()
                self.assertIsNone(outcome.summary)
                self.assertIsNone(app.job_continuation)
                self.assertIsNotNone(app.current_job_run)
        await app.stop()

    async def test_old_and_unrelated_events_do_not_satisfy_wait(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "A", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
        ))
        app = self.app(backend)
        await app.start()
        await self.publish_presence(app)
        app.start_job_run(self.store.create_job("Wait").id)
        await app.work_current_job_once()
        await app.events.publish(BodyOrientationChanged(
            source="test", previous_yaw_degrees=0, previous_pitch_degrees=0,
            yaw_degrees=1, pitch_degrees=0,
        ))
        await self.drain()
        self.assertFalse(app.job_continuation.event_satisfied)
        await app.stop()

    async def test_event_is_sticky_while_paused_and_diagnostic_is_bounded(self):
        app, backend = await self.start_waiting((
            {"disposition": "continue", "summary": "A", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "B", "readiness": "ready",
             "delay_seconds": None, "event_type": None},
        ))
        app.pause_task()
        await self.publish_presence(app)
        app._offer_job_continuation()
        self.assertTrue(app.job_continuation.event_satisfied)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        output = RuntimeConsole(app).execute("job current")[0]
        self.assertIn("waiting_event: presence_changed", output)
        self.assertIn("event_satisfied: true", output)
        app.resume_task()
        app._offer_job_continuation()
        await self.drain()
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        await app.stop()

    async def test_manual_work_overrides_wait_without_fake_wake_context(self):
        app, backend = await self.start_waiting((
            {"disposition": "continue", "summary": "A", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "B", "readiness": "ready",
             "delay_seconds": None, "event_type": None},
        ))
        await app.work_current_job_once()
        self.assertIn("Previous work summary", backend.requests[2][1])
        self.assertNotIn("continuation_wake_event", backend.requests[2][1])
        self.assertIs(app.job_continuation.readiness, JobContinuationReadiness.READY)
        await app.stop()

    async def test_exhausted_budget_is_not_refilled_or_bypassed_by_event(self):
        app, backend = await self.start_waiting((
            {"disposition": "continue", "summary": "A", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "B", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "C", "readiness": "ready",
             "delay_seconds": None, "event_type": None},
        ), max_steps=1)
        await self.publish_presence(app)
        app._offer_job_continuation()
        await self.drain()
        continuation = app.job_continuation
        self.assertIs(continuation.state, JobContinuationState.AWAITING_OPERATOR)
        self.assertIs(continuation.readiness, JobContinuationReadiness.WAIT_FOR_EVENT)
        self.assertIs(continuation.event_type, JobReadinessEventType.PRESENCE_CHANGED)
        self.assertFalse(continuation.event_satisfied)
        self.assertEqual(continuation.automatic_steps_remaining, 0)
        request_count = len(backend.requests)
        await self.publish_presence(app, False)
        self.assertTrue(app.job_continuation.event_satisfied)
        self.assertIs(app.job_continuation.state, JobContinuationState.AWAITING_OPERATOR)
        for _ in range(3):
            app._offer_job_continuation()
        await self.drain()
        self.assertEqual(len(backend.requests), request_count)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 0)
        await app.work_current_job_once()
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 1)
        await app.stop()

    async def test_provider_failure_after_acceptance_consumes_wake(self):
        class FailAcceptedWakeBackend(ReadinessBackend):
            async def respond(self, message, **kwargs):
                if len(self.requests) == 2:
                    self.requests.append((message, kwargs.get("instructions"), ()))
                    raise RuntimeError("provider failed")
                return await super().respond(message, **kwargs)

        backend = FailAcceptedWakeBackend((
            {"disposition": "continue", "summary": "A", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
        ))
        app = self.app(backend)
        await app.start()
        app.start_job_run(self.store.create_job("Failure").id)
        await app.work_current_job_once()
        await self.publish_presence(app)
        app._offer_job_continuation()
        self.assertFalse(app.job_continuation.event_satisfied)
        self.assertIsNone(app.job_continuation.wake_event)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        await self.drain()
        self.assertIs(app.job_continuation.state, JobContinuationState.AWAITING_OPERATOR)
        request_count = len(backend.requests)
        for _ in range(3):
            app._offer_job_continuation()
        await self.drain()
        self.assertEqual(len(backend.requests), request_count)
        self.assertFalse(app.job_continuation.event_satisfied)
        self.assertIsNone(app.job_continuation.wake_event)
        await app.stop()

    async def test_terminalization_clears_satisfied_wake(self):
        backend = ReadinessBackend(tuple(
            {"disposition": "continue", "summary": "A",
             "readiness": "wait_for_event", "delay_seconds": None,
             "event_type": "presence_changed"}
            for _ in range(2)
        ))
        app = self.app(backend)
        await app.start()
        for status in (JobRunStatus.COMPLETED, JobRunStatus.STOPPED):
            with self.subTest(status=status.value):
                job = self.store.create_job(f"Terminal {status.value}")
                app.start_job_run(job.id)
                await app.work_current_job_once()
                app.pause_task()
                await self.publish_presence(app)
                self.assertIsNotNone(app.job_continuation.wake_event)
                request_count = len(backend.requests)
                app.resume_task()
                app.finish_job_run(status, "terminal")
                self.assertIsNone(app.job_continuation)
                app._offer_job_continuation()
                await self.publish_presence(app, False)
                self.assertEqual(len(backend.requests), request_count)
        await app.stop()

    async def test_stale_run_event_cannot_cross_occurrence_boundary(self):
        app, backend = await self.start_waiting((
            {"disposition": "continue", "summary": "RUN1", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "RUN2", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
        ))
        job = app.current_job_run.job
        run1 = app.current_job_run.run.id
        stale_event = PresenceChanged(
            source="test", previous_present=False, present=True,
        )
        app.finish_job_run(JobRunStatus.STOPPED, "replace")
        run2 = app.start_job_run(job.id)
        self.assertNotEqual(run1, run2.run.id)
        self.assertIsNone(app.job_continuation)
        await app._on_job_presence_changed(stale_event)
        self.assertIsNone(app.job_continuation)
        await app.work_current_job_once()
        continuation = app.job_continuation
        self.assertEqual(continuation.last_summary, "RUN2")
        self.assertFalse(continuation.event_satisfied)
        self.assertIsNone(continuation.wake_event)
        self.assertGreater(continuation.event_armed_after_ns, stale_event.timestamp_ns)
        await app._on_job_presence_changed(stale_event)
        self.assertFalse(app.job_continuation.event_satisfied)
        await self.publish_presence(app)
        self.assertEqual(len(backend.requests), 6)
        self.assertFalse(app.job_continuation.event_satisfied)
        await app.stop()

    async def test_scheduled_occurrence_waits_for_event_without_new_run(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "A", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
            {"disposition": "continue", "summary": "B", "readiness": "ready",
             "delay_seconds": None, "event_type": None},
        ))
        job = self.store.create_job("Scheduled wait")
        self.store.set_schedule(job.id, "00:00", "UTC")
        app = self.app(backend)
        await app.start()
        await app._offer_scheduled_job()
        await app._active_job_work_task
        run = app.current_job_run
        marker = self.store.get_schedule(job.id).last_started_local_date
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.assertIs(run.run.status, JobRunStatus.RUNNING)
        self.assertIs(run.task.status, TaskStatus.RUNNING)
        self.assertIs(app.job_continuation.readiness,
                      JobContinuationReadiness.WAIT_FOR_EVENT)
        self.assertFalse(app.job_continuation.event_satisfied)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 3)
        request_count = len(backend.requests)
        app._offer_job_continuation()
        await self.drain()
        self.assertEqual(len(backend.requests), request_count)
        await self.publish_presence(app)
        await self.drain()
        self.assertEqual(app.current_job_run.run.id, run.run.id)
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.assertEqual(self.store.get_schedule(job.id).last_started_local_date, marker)
        self.assertEqual(app.job_continuation.automatic_steps_remaining, 2)
        self.assertIn("Previous work summary", backend.requests[2][1])
        self.assertIn("continuation_wake_event: presence_changed", backend.requests[2][1])
        await app.stop()

    async def test_shutdown_clears_wait_and_listener_workers(self):
        app, backend = await self.start_waiting((
            {"disposition": "continue", "summary": "A", "readiness": "wait_for_event",
             "delay_seconds": None, "event_type": "presence_changed"},
        ))
        subscription = app._job_event_subscription
        app.pause_task()
        await self.publish_presence(app)
        self.assertTrue(app.job_continuation.event_satisfied)
        self.assertIsNotNone(app.job_continuation.wake_event)
        request_count = len(backend.requests)
        await app.stop()
        self.assertIsNone(app.job_continuation)
        self.assertIsNone(app.current_job_run)
        self.assertFalse(app.events.is_running)
        self.assertTrue(subscription._closed)
        self.assertIsNone(subscription._task)
        await app._on_job_presence_changed(PresenceChanged(
            source="test", previous_present=True, present=False,
        ))
        self.assertEqual(len(backend.requests), request_count)


if __name__ == "__main__":
    unittest.main()
