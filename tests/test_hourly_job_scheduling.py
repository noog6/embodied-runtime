"""Durable wall-clock recurrence through SQLite and the ordinary Job runtime."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.console import RuntimeConsole
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    FindingEvidence, FindingEvidenceClass, FindingKind, JobRunStatus,
    JobSchedule, JobTriggerType, SQLiteJobStore,
)
from embodied_runtime.jobs.focus import JobFocusProposal
from embodied_runtime.profile import RobotProfile
from tests.test_job_execution import BlockingBackend, JobBackend, Platform
from tests.test_job_focus import FocusBackend, proposal
from tests.test_job_readiness import ReadinessBackend
from tests.test_scheduled_job_activation import MutableClock


def instant(value):
    return datetime.fromisoformat(value).astimezone(UTC)


class HourlyScheduleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.clock = MutableClock(instant("2026-10-01T12:45:00Z"))
        self.store = SQLiteJobStore(self.path, clock=self.clock)
        self.addCleanup(self.store.close)
        self.job = self.store.create_job("Recurring")

    def schedule(self, **kwargs):
        return self.store.set_schedule(self.job.id, None, "America/Toronto",
                                       cadence="hourly", **kwargs)

    def admit(self, schedule=None):
        schedule = schedule or self.store.get_schedule(self.job.id)
        return self.store.create_scheduled_run(
            self.job.id, schedule.due_slot(self.clock()),
            expected_schedule=schedule, clock=self.clock)

    def test_hourly_defaults_validation_and_conflicts(self):
        self.assertEqual(self.schedule().minute, 0)
        self.assertEqual(self.schedule(minute=59).minute, 59)
        for minute in (-1, 60, True, 1.5, "45"):
            with self.subTest(minute=minute), self.assertRaises(ValueError):
                self.schedule(minute=minute)
        for kwargs in ({"cadence": "weekly"}, {"minute": 0}):
            with self.assertRaises(ValueError):
                JobSchedule(1, True, "12:45", "UTC", **kwargs)
        with self.assertRaises(ValueError):
            JobSchedule(1, True, "12:45", "UTC", cadence="hourly")
        with self.assertRaises(ValueError):
            JobSchedule(1, True, None, "Invalid/Zone", cadence="hourly")
        with self.assertRaises(ValueError):
            replace(self.schedule(), last_started_slot=datetime(2026, 1, 1))

    def test_two_hour_rollback_selects_latest_instant_across_later_local_labels(self):
        schedule = JobSchedule(1, True, None, "Antarctica/Troll", cadence="hourly", minute=45)
        for now, expected in (
            ("2026-10-25T00:44:59Z", "2026-10-24T23:45:00Z"),
            ("2026-10-25T00:45:00Z", "2026-10-25T00:45:00Z"),
            ("2026-10-25T00:59:59Z", "2026-10-25T00:45:00Z"),
            ("2026-10-25T01:00:00Z", "2026-10-25T00:45:00Z"),
            ("2026-10-25T01:15:00Z", "2026-10-25T00:45:00Z"),
            ("2026-10-25T01:44:59Z", "2026-10-25T00:45:00Z"),
            ("2026-10-25T01:45:00Z", "2026-10-25T01:45:00Z"),
            ("2026-10-25T02:44:59Z", "2026-10-25T01:45:00Z"),
            ("2026-10-25T02:45:00Z", "2026-10-25T02:45:00Z"),
        ):
            with self.subTest(now=now):
                self.assertEqual(schedule.due_slot(instant(now)), instant(expected))

    def test_atomic_admission_revalidates_schedule_job_and_live_runs(self):
        original = self.schedule(minute=45)
        self.schedule(minute=30)
        self.assertIsNone(self.admit(original))
        current = self.store.get_schedule(self.job.id)
        self.store.set_job_enabled(self.job.id, False)
        self.assertIsNone(self.admit(current))
        self.store.set_job_enabled(self.job.id, True)
        self.store.remove_schedule(self.job.id)
        self.assertIsNone(self.admit(current))
        current = self.schedule(minute=45, enabled=False)
        self.assertIsNone(self.admit(current))
        current = self.schedule(minute=45)
        live = self.store.create_run(self.job.id)
        for status in (JobRunStatus.PENDING, JobRunStatus.RUNNING):
            if status is JobRunStatus.RUNNING:
                self.store.transition_run(live.id, status)
            self.assertIsNone(self.admit(current))
            self.assertIsNone(self.store.get_schedule(self.job.id).last_started_slot)
        self.store.transition_run(live.id, JobRunStatus.STOPPED)
        self.assertIsNotNone(self.admit())
        with self.assertRaises(RuntimeError):
            self.store.create_run(self.job.id, require_idle=True)
        self.assertIsNone(self.store.create_triggered_run(self.job.id))

    def test_two_connections_race_same_and_different_slots_without_overlap(self):
        self.schedule(minute=45)
        barrier = threading.Barrier(2)

        def admit(offset):
            with_store = SQLiteJobStore(self.path, clock=self.clock)
            try:
                now = self.clock() + timedelta(hours=offset)
                schedule = with_store.get_schedule(self.job.id)
                barrier.wait(timeout=5)
                return with_store.create_scheduled_run(
                    self.job.id, schedule.due_slot(now), expected_schedule=schedule, clock=lambda: now)
            finally:
                with_store.close()

        for offsets in ((0, 0), (0, 1)):
            with self.subTest(offsets=offsets):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(admit, offsets))
                self.assertEqual(sum(run is not None for run in results), 1)
                run = next(run for run in results if run is not None)
                self.store.transition_run(run.id, JobRunStatus.STOPPED)
                self.clock.value += timedelta(hours=2)

    def test_slot_must_be_latest_due_and_aware(self):
        schedule = self.schedule(minute=45)
        for slot in (self.clock() - timedelta(hours=1), self.clock() + timedelta(hours=1)):
            self.assertIsNone(self.store.create_scheduled_run(
                self.job.id, slot, expected_schedule=schedule, clock=self.clock))
        with self.assertRaises(ValueError):
            self.store.create_scheduled_run(self.job.id, datetime(2026, 1, 1))
        self.assertIsNone(self.store.create_scheduled_run(self.job.id, "2026-10-01"))

    def test_manual_admission_racing_scheduler_cannot_overlap(self):
        self.schedule(minute=45)
        barrier = threading.Barrier(2)

        def admit(manual):
            store = SQLiteJobStore(self.path, clock=self.clock)
            try:
                schedule = store.get_schedule(self.job.id)
                barrier.wait(timeout=5)
                if manual:
                    try:
                        return store.create_run(self.job.id, require_idle=True)
                    except RuntimeError:
                        return None
                return store.create_scheduled_run(self.job.id, schedule.due_slot(self.clock()),
                                                   expected_schedule=schedule, clock=self.clock)
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(admit, (True, False)))
        self.assertEqual(sum(run is not None for run in results), 1)
        self.assertEqual(len(self.store.list_runs(self.job.id)), 1)

    def test_migration_preserves_v10_catalog_history_findings_markers_and_focus(self):
        schedule = self.store.set_schedule(self.job.id, "08:00", "America/Toronto")
        run = self.store.create_scheduled_run(self.job.id, "2026-10-01")
        self.store.transition_run(run.id, JobRunStatus.RUNNING)
        evidence = FindingEvidence(1, "inspect_runtime", "accepted",
                                   FindingEvidenceClass.RUNTIME_INSPECTION)
        finding = self.store.create_finding(self.job.id, run.id, "task-1", 1,
                                            "topic", FindingKind.OBSERVATION, "claim", (evidence,))
        run = self.store.transition_run(run.id, JobRunStatus.COMPLETED,
                                        outcome_summary="done", result_report="preserved report")
        self.store.set_trigger(self.job.id, JobTriggerType.RUNTIME_READY)
        self.store.set_carry_forward_enabled(self.job.id, True)
        # Compute the legacy identity independently of the new implementation.
        legacy = hashlib.sha256(json.dumps({
            "name": self.job.name, "description": self.job.description, "target": None,
            "triggers": [("runtime_ready", True)],
            "schedule": (True, "08:00", "America/Toronto"),
        }, sort_keys=True).encode()).hexdigest()
        focus = self.store.administer_focus(self.job.id, JobFocusProposal(**proposal()), run.id)
        self.assertEqual(focus.definition_identity, legacy)
        expected = (self.store.list_jobs(), self.store.list_runs(self.job.id),
                    self.store.list_findings(), self.store.list_triggers(),
                    self.store.get_schedule(self.job.id), focus)
        # Rebuild only the schedule table as the actual v10 shape.
        self.store._connection.executescript("""
            DROP TABLE job_run_procedures;
            DROP TABLE job_procedures;
            ALTER TABLE jobs DROP COLUMN procedure_enabled;
            ALTER TABLE jobs DROP COLUMN procedure_experiment;
            ALTER TABLE jobs DROP COLUMN procedure_version;
            ALTER TABLE job_schedules RENAME TO schedules_new;
            CREATE TABLE job_schedules (
                job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE RESTRICT,
                enabled INTEGER NOT NULL, local_time TEXT NOT NULL, timezone TEXT NOT NULL,
                last_started_local_date TEXT);
            INSERT INTO job_schedules SELECT job_id,enabled,local_time,timezone,
                last_started_local_date FROM schedules_new;
            DROP TABLE schedules_new;
            PRAGMA user_version=10;
        """)
        with_store = SQLiteJobStore(self.path, clock=self.clock)
        try:
            actual = (with_store.list_jobs(), with_store.list_runs(self.job.id),
                      with_store.list_findings(), with_store.list_triggers(),
                      with_store.get_schedule(self.job.id), with_store.get_focus(self.job.id))
            self.assertEqual(actual, expected)
            self.assertEqual(with_store.get_finding(finding.id), finding)
            self.assertEqual(with_store.focus_definition_identity(self.job.id), legacy)
            self.assertEqual(with_store.get_schedule(self.job.id).last_started_local_date,
                             "2026-10-01")
            self.assertIsNone(with_store.get_schedule(self.job.id).last_started_slot)
            self.assertEqual(len(with_store.list_runs(self.job.id)), 1)
            self.assertEqual(schedule.cadence, "daily")
        finally:
            with_store.close()


class HourlyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.clock = MutableClock(instant("2026-10-01T12:45:00Z"))
        self.store = SQLiteJobStore(self.path, clock=self.clock)
        self.apps = []

    async def asyncTearDown(self):
        for app in reversed(self.apps):
            await app.stop()
        self.store.close()
        self.temp.cleanup()

    async def app(self, backend=None, *, capacity=1, auto_continue=False, store=None):
        async def dormant(_):
            await asyncio.Event().wait()

        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True, jobs_max_concurrent_work=capacity,
                               jobs_auto_continue=auto_continue),
            cognition_backend=backend or JobBackend("completed"), platform_provider=Platform(),
            job_store=store or self.store, timezone_name="America/Toronto",
            wall_clock=self.clock, job_scheduler_sleep=dormant,
        )
        self.apps.append(app)
        await app.start()
        return app

    def hourly(self, *, minute=45, timezone="America/Toronto", enabled=True):
        job = self.store.create_job("Hourly", enabled=enabled)
        self.store.set_schedule(job.id, None, timezone, cadence="hourly", minute=minute)
        return job

    async def poll(self, app):
        await app._offer_scheduled_job()
        tasks = [context.active_work_task for context in app.job_execution_contexts
                 if context.active_work_task is not None]
        await asyncio.gather(*tasks)

    async def test_multiple_hours_one_date_repeated_polls_restart_and_coalescing(self):
        app = await self.app()
        job = self.hourly()
        await self.poll(app)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.clock.value += timedelta(hours=1)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 2)
        await app.stop()
        self.store = SQLiteJobStore(self.path, clock=self.clock)
        app = await self.app()
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 2)
        self.clock.value += timedelta(days=9, hours=4, minutes=10)
        await self.poll(app)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 3)
        self.assertEqual(self.store.get_schedule(job.id).last_started_slot,
                         self.clock().replace(minute=45))

    async def test_startup_before_minute_considers_previous_hour_latest_slot(self):
        job = self.hourly()
        self.clock.value = instant("2026-10-01T12:10:00Z")
        app = await self.app()
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.assertEqual(self.store.get_schedule(job.id).last_started_slot,
                         instant("2026-10-01T11:45:00Z"))

    async def test_forward_backward_clock_changes_use_durable_high_water(self):
        app = await self.app()
        job = self.hourly()
        for value, count in (("2026-10-01T12:45:00Z", 1),
                             ("2026-10-01T17:50:00Z", 2),
                             ("2026-10-01T13:50:00Z", 2),
                             ("2026-10-01T17:50:00Z", 2),
                             ("2026-10-01T18:44:59Z", 2),
                             ("2026-10-01T18:45:00Z", 3)):
            self.clock.value = instant(value)
            await self.poll(app)
            self.assertEqual(len(self.store.list_runs(job.id)), count)

    async def test_admission_rechecks_clock_after_poll_snapshot(self):
        app = await self.app()
        job = self.hourly()
        later = self.clock() + timedelta(hours=1)
        with patch.object(app, "_aware_wall_clock", side_effect=(self.clock(), later)):
            await app._offer_scheduled_job()
        self.assertEqual(self.store.list_runs(job.id), ())
        self.assertIsNone(self.store.get_schedule(job.id).last_started_slot)
        self.clock.value = later
        await self.poll(app)
        self.assertEqual(self.store.get_schedule(job.id).last_started_slot, later)

    async def test_operator_contention_defers_current_slot_and_coalesces_forward(self):
        app = await self.app()
        job = self.hourly()
        app.episode_coordinator._operator_waiters = 1
        await self.poll(app)
        self.assertEqual(self.store.list_runs(job.id), ())
        self.assertIsNone(self.store.get_schedule(job.id).last_started_slot)
        self.clock.value += timedelta(hours=5)
        await self.poll(app)
        app.episode_coordinator._operator_waiters = 0
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.assertEqual(self.store.get_schedule(job.id).last_started_slot, self.clock())

    async def test_active_operator_and_general_attention_defer_without_consuming(self):
        app = await self.app()
        job = self.hourly()
        coordinator = app.episode_coordinator
        operator = await coordinator.start_operator("console", "operator request")
        await self.poll(app)
        self.assertEqual(self.store.list_runs(job.id), ())
        coordinator.close(operator, "handled")
        general = coordinator.try_start("test", "test", "general work", None)
        self.assertIsNotNone(general)
        await self.poll(app)
        self.assertEqual(self.store.list_runs(job.id), ())
        self.assertIsNone(self.store.get_schedule(job.id).last_started_slot)
        coordinator.close(general, "handled")
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 1)

    async def test_capacity_and_same_job_running_do_not_consume_new_slot(self):
        backend = BlockingBackend()
        app = await self.app(backend)
        first = self.hourly()
        second = self.hourly()
        await app._offer_scheduled_job()
        await backend.started.wait()
        self.clock.value += timedelta(hours=2)
        await app._offer_scheduled_job()
        self.assertEqual(len(self.store.list_runs(first.id)), 1)
        self.assertEqual(self.store.list_runs(second.id), ())
        self.assertIsNone(self.store.get_schedule(second.id).last_started_slot)
        self.assertEqual(self.store.get_schedule(first.id).last_started_slot,
                         self.clock() - timedelta(hours=2))
        await app.stop()
        self.store = SQLiteJobStore(self.path, clock=self.clock)
        app = await self.app()
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(first.id)), 2)
        self.assertEqual(len(self.store.list_runs(second.id)), 0)
        # Equal latest slots are ordered by Job ID; the next poll admits second.
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(first.id)), 2)
        self.assertEqual(len(self.store.list_runs(second.id)), 1)

    async def test_parked_same_job_does_not_block_independent_job(self):
        backend = ReadinessBackend((
            {"disposition": "continue", "summary": "park",
             "readiness": "wait_for_operator", "delay_seconds": None, "event_type": None},
            {"disposition": "completed", "summary": "other done",
             "readiness": None, "delay_seconds": None, "event_type": None},
        ))
        app = await self.app(backend, auto_continue=True)
        first = self.hourly()
        second = self.hourly()
        await self.poll(app)
        self.assertEqual(app.job_execution_contexts[0].execution_state, "parked")
        self.clock.value += timedelta(hours=1)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(first.id)), 1)
        self.assertEqual(len(self.store.list_runs(second.id)), 1)
        self.assertIs(self.store.list_runs(second.id)[0].status, JobRunStatus.COMPLETED)
        self.assertEqual(self.store.get_schedule(first.id).last_started_slot,
                         self.clock() - timedelta(hours=1))

    async def test_independent_jobs_can_use_separate_execution_slots(self):
        backend = BlockingBackend()
        app = await self.app(backend, capacity=2)
        jobs = (self.hourly(), self.hourly())
        await app._offer_scheduled_job()
        await backend.started.wait()
        await app._offer_scheduled_job()
        self.assertEqual(app.job_work_slots_occupied, 2)
        self.assertEqual([len(self.store.list_runs(job.id)) for job in jobs], [1, 1])
        await app._offer_scheduled_job()
        self.assertEqual(app.job_work_slots_occupied, 2)

    async def test_manual_same_job_live_defers_but_another_job_can_start(self):
        app = await self.app(capacity=2)
        first, second = self.hourly(), self.hourly()
        app.start_job_run(first.id)
        self.clock.value += timedelta(hours=2)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(first.id)), 1)
        self.assertIsNone(self.store.get_schedule(first.id).last_started_slot)
        self.assertEqual(len(self.store.list_runs(second.id)), 1)

    async def test_durable_live_run_without_context_does_not_starve_independent_job(self):
        app = await self.app()
        first, second = self.hourly(), self.hourly()
        self.store.create_run(first.id)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(first.id)), 1)
        self.assertIsNone(self.store.get_schedule(first.id).last_started_slot)
        self.assertEqual(len(self.store.list_runs(second.id)), 1)

    async def test_disabled_removed_and_replaced_schedules(self):
        app = await self.app()
        disabled = self.hourly(enabled=False)
        removed = self.hourly()
        replaced = self.hourly()
        self.store.remove_schedule(removed.id)
        self.store.set_schedule(replaced.id, "23:00", "America/Toronto")
        await self.poll(app)
        self.assertTrue(all(not self.store.list_runs(job.id)
                            for job in (disabled, removed, replaced)))
        self.store.set_job_enabled(disabled.id, True)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(disabled.id)), 1)
        self.assertEqual(self.store.get_schedule(replaced.id).cadence, "daily")

    async def test_daily_replacement_and_cadence_changes_preserve_markers(self):
        app = await self.app()
        job = self.store.create_job("Replace")
        self.store.set_schedule(job.id, "08:45", "America/Toronto")
        await self.poll(app)
        self.store.set_schedule(job.id, "08:00", "America/Toronto")
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.store.set_schedule(job.id, None, "America/Toronto", cadence="hourly", minute=45)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 2)
        marker = self.store.get_schedule(job.id).last_started_slot
        self.store.set_schedule(job.id, None, "America/Toronto", cadence="hourly", minute=30)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 2)
        self.store.set_schedule(job.id, "08:00", "America/Toronto")
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 2)
        self.assertEqual(self.store.get_schedule(job.id).last_started_slot, marker)
        self.assertEqual(self.store.get_schedule(job.id).last_started_local_date, "2026-10-01")

    async def test_slot_update_insert_and_commit_failures_leave_no_admission(self):
        app = await self.app()
        job = self.hourly()
        connection = self.store._connection
        for failure in ("marker", "run", "commit"):
            with self.subTest(failure=failure):
                if failure == "commit":
                    connection.set_authorizer(lambda action, name, *rest:
                        sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION
                        and name == "COMMIT" else sqlite3.SQLITE_OK)
                else:
                    table, action = (("job_hourly_activation", "INSERT") if failure == "marker"
                                     else ("job_runs", "INSERT"))
                    connection.execute(f"""CREATE TRIGGER fail_admission BEFORE {action}
                        ON {table} BEGIN SELECT RAISE(ABORT,'injected failure'); END""")
                try:
                    with self.assertRaises(sqlite3.DatabaseError):
                        await app._offer_scheduled_job()
                finally:
                    connection.set_authorizer(None)
                    connection.execute("DROP TRIGGER IF EXISTS fail_admission")
                self.assertEqual(self.store.list_runs(job.id), ())
                self.assertIsNone(self.store.get_schedule(job.id).last_started_slot)
                self.assertEqual(app.job_execution_contexts, ())
                self.assertIsNone(app.current_task)
                self.assertIsNone(app.active_goal)
                self.assertFalse(connection.in_transaction)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 1)

    async def test_cancelled_running_and_orphan_pending_are_not_retried_on_restart(self):
        backend = BlockingBackend()
        app = await self.app(backend)
        running = self.hourly()
        await app._offer_scheduled_job()
        await backend.started.wait()
        await app.stop()
        self.store = SQLiteJobStore(self.path, clock=self.clock)
        pending = self.hourly()
        schedule = self.store.get_schedule(pending.id)
        orphan = self.store.create_scheduled_run(pending.id, schedule.due_slot(self.clock()),
                                                clock=self.clock, expected_schedule=schedule)
        app = await self.app()
        await self.poll(app)
        self.assertIs(self.store.get_run(orphan.id).status, JobRunStatus.INTERRUPTED)
        for job in (running, pending):
            self.assertEqual(len(self.store.list_runs(job.id)), 1)
            self.assertIs(self.store.list_runs(job.id)[0].status, JobRunStatus.INTERRUPTED)
        self.clock.value += timedelta(hours=1)
        await self.poll(app)
        await self.poll(app)
        self.assertEqual([len(self.store.list_runs(job.id)) for job in (running, pending)], [2, 2])

    async def test_failed_and_stopped_runs_are_not_retried_in_same_slot(self):
        app = await self.app(JobBackend("failed"))
        job = self.hourly()
        await self.poll(app)
        self.assertIs(self.store.list_runs(job.id)[0].status, JobRunStatus.FAILED)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        stopped = self.hourly()
        schedule = self.store.get_schedule(stopped.id)
        run = self.store.create_scheduled_run(stopped.id, schedule.due_slot(self.clock()),
                                             expected_schedule=schedule, clock=self.clock)
        self.store.transition_run(run.id, JobRunStatus.STOPPED)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(stopped.id)), 1)

    async def test_unschedule_readd_does_not_reset_hourly_consumption(self):
        app = await self.app()
        job = self.hourly()
        await self.poll(app)
        self.store.remove_schedule(job.id)
        self.assertIsNone(self.store.get_schedule(job.id))
        self.store.set_schedule(job.id, None, "America/Toronto", cadence="hourly", minute=45)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.clock.value += timedelta(hours=1)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 2)

    async def test_toronto_fall_back_distinct_instants_and_spring_gap(self):
        app = await self.app()
        job = self.hourly()
        for value, marker, count in (
            ("2026-11-01T05:45:00Z", "2026-11-01T05:45:00Z", 1),
            ("2026-11-01T06:30:00Z", "2026-11-01T05:45:00Z", 1),
            ("2026-11-01T06:45:00Z", "2026-11-01T06:45:00Z", 2),
            ("2027-03-14T06:45:00Z", "2027-03-14T06:45:00Z", 3),
            ("2027-03-14T07:30:00Z", "2027-03-14T06:45:00Z", 3),
            ("2027-03-14T07:45:00Z", "2027-03-14T07:45:00Z", 4),
        ):
            self.clock.value = instant(value)
            await self.poll(app)
            self.assertEqual(len(self.store.list_runs(job.id)), count)
            self.assertEqual(self.store.get_schedule(job.id).last_started_slot, instant(marker))

    async def test_two_hour_rollback_admits_latest_slot_on_startup(self):
        self.clock.value = instant("2026-10-25T01:15:00Z")
        job = self.hourly(timezone="Antarctica/Troll")
        app = await self.app()
        await self.poll(app)
        self.assertEqual(self.store.get_schedule(job.id).last_started_slot,
                         instant("2026-10-25T00:45:00Z"))
        run, = self.store.list_runs(job.id)
        self.assertIs(run.status, JobRunStatus.COMPLETED)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 1)

    async def test_two_hour_rollback_consumed_older_slot_does_not_hide_latest(self):
        app = await self.app()
        job = self.hourly(timezone="Antarctica/Troll")
        for now, expected, count in (
            ("2026-10-24T23:45:00Z", "2026-10-24T23:45:00Z", 1),
            ("2026-10-25T01:15:00Z", "2026-10-25T00:45:00Z", 2),
            ("2026-10-25T01:44:59Z", "2026-10-25T00:45:00Z", 2),
            ("2026-10-25T01:45:00Z", "2026-10-25T01:45:00Z", 3),
        ):
            self.clock.value = instant(now)
            await self.poll(app)
            self.assertEqual(len(self.store.list_runs(job.id)), count)
            self.assertEqual(self.store.get_schedule(job.id).last_started_slot, instant(expected))
        await app.stop()
        self.store = SQLiteJobStore(self.path, clock=self.clock)
        app = await self.app()
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 3)

    async def test_non_whole_hour_timezone_uses_local_minute(self):
        app = await self.app()
        self.clock.value = instant("2026-10-01T12:00:00Z")  # Kathmandu 17:45
        job = self.hourly(timezone="Asia/Kathmandu")
        await self.poll(app)
        self.assertEqual(self.store.get_schedule(job.id).last_started_slot, self.clock())
        self.clock.value += timedelta(minutes=59, seconds=59)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.clock.value += timedelta(seconds=1)
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 2)

    async def test_daily_dst_behavior_still_runs_gap_day_and_once_per_fall_date(self):
        app = await self.app()
        job = self.store.create_job("Daily DST")
        self.store.set_schedule(job.id, "01:45", "America/Toronto")
        for value in ("2026-11-01T05:45:00Z", "2026-11-01T06:45:00Z"):
            self.clock.value = instant(value)
            await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 1)
        self.store.set_schedule(job.id, "02:45", "America/Toronto")
        self.clock.value = instant("2027-03-14T07:01:00Z")
        await self.poll(app)
        self.assertEqual(len(self.store.list_runs(job.id)), 2)

    async def test_carry_forward_identity_accepts_unchanged_daily_withholds_hourly_change(self):
        backend = FocusBackend(proposal())
        app = await self.app(backend)
        job = self.store.create_job("Focus")
        self.store.set_carry_forward_enabled(job.id, True)
        self.store.set_schedule(job.id, "08:45", "America/Toronto")
        await self.poll(app)
        original = self.store.focus_definition_identity(job.id)
        self.store.set_schedule(job.id, "08:45", "America/Toronto")
        self.assertEqual(self.store.focus_definition_identity(job.id), original)
        backend.focus = None
        self.clock.value += timedelta(days=1)
        await self.poll(app)
        self.assertIn("Carry-forward focus (", backend.requests[2][1])
        self.assertEqual(self.store.focus_definition_identity(job.id), original)
        self.store.set_schedule(job.id, None, "America/Toronto", cadence="hourly", minute=45)
        hourly = self.store.focus_definition_identity(job.id)
        self.assertNotEqual(hourly, original)
        await self.poll(app)
        self.assertNotIn("Carry-forward focus (", backend.requests[4][1])
        self.assertEqual(self.store.focus_definition_identity(job.id), hourly)
        self.store.set_schedule(job.id, None, "America/Toronto", cadence="hourly", minute=30)
        self.assertNotEqual(self.store.focus_definition_identity(job.id), hourly)

    async def test_console_inspection_validation_replacement_and_persistence_acknowledgements(self):
        backend = JobBackend("completed")
        app = await self.app(backend)
        job = self.store.create_job("Console")
        console = RuntimeConsole(app)

        def command(value):
            return console.execute(value)[0]

        self.assertIn("has no schedule", command(f"job schedule JOB{job.id}"))
        self.assertEqual(command(f"job schedule JOB{job.id} hourly"),
                         f"Scheduled JOB{job.id} hourly at minute 0 America/Toronto.")
        self.assertEqual(self.store.get_schedule(job.id).minute, 0)
        self.assertIn("minute 45", command(f"job schedule JOB{job.id} hourly --minute 45 "
                                           "--timezone America/Toronto"))
        original = self.store.get_schedule(job.id)
        for suffix in ("hourly --minute -1", "hourly --minute 60", "hourly --minute 1.0",
                       "hourly --minute true", "hourly --minute", "hourly 12:45",
                       "hourly --minute 2 --minute 3", "hourly --timezone UTC --timezone UTC",
                       "hourly --timezone Invalid/Zone", "hourly --timezone",
                       "hourly --interval 60", "daily 12:45 --minute 45", "daily 2:00"):
            with self.subTest(suffix=suffix):
                response = command(f"job schedule JOB{job.id} {suffix}")
                self.assertTrue(response.startswith(("Unable", "Usage")), response)
                self.assertEqual(self.store.get_schedule(job.id), original)
        self.assertEqual(backend.requests, [])
        with patch.object(self.store, "get_schedule", side_effect=sqlite3.OperationalError("read failure")):
            self.assertIn("persistence operation failed", command(f"job schedule JOB{job.id}"))
        await self.poll(app)
        inspected = command(f"job schedule JOB{job.id}")
        for value in ("cadence:           hourly", "minute:            45",
                      "timezone:          America/Toronto", "2026-10-01T12:45:00+00:00"):
            self.assertIn(value, inspected)
        with patch.object(self.store, "set_schedule", side_effect=sqlite3.OperationalError("disk full")):
            self.assertEqual(command(f"job schedule JOB{job.id} hourly --minute 30"),
                             "Unable to schedule Job: persistence operation failed.")
        self.assertEqual(self.store.get_schedule(job.id).minute, 45)
        self.assertIn("daily at 12:45", command(f"job schedule JOB{job.id} daily 12:45 "
                                               "--timezone America/Toronto"))
        self.assertIn("local_time:        12:45", command(f"job schedule JOB{job.id}"))
        with patch.object(self.store, "remove_schedule", side_effect=sqlite3.OperationalError("disk full")):
            self.assertEqual(command(f"job unschedule JOB{job.id}"),
                             "Unable to unschedule Job: persistence operation failed.")
        self.assertIsNotNone(self.store.get_schedule(job.id))
        self.assertEqual(command(f"job unschedule JOB{job.id}"), f"Unscheduled JOB{job.id}.")
        self.assertIn("has no schedule", command(f"job unschedule JOB{job.id}"))


if __name__ == "__main__":
    unittest.main()
