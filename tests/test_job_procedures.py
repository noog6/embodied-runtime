"""Procedure vertical slice: real lifecycle/SQLite and deterministic fake providers."""

import asyncio
from contextlib import closing
from dataclasses import asdict, FrozenInstanceError
from datetime import timedelta
import json
import shlex
import sqlite3
import unittest
from unittest.mock import patch

from embodied_runtime.app import JOB_OUTCOME_EVALUATION_REQUEST, REPORT_JOB_OUTCOME_TOOL
from embodied_runtime.cognition import CognitionToolCall
from embodied_runtime.console import RuntimeConsole
from embodied_runtime.jobs import JobRunStatus, SQLiteJobStore, JobTriggerType
from embodied_runtime.jobs.procedure import (
    CONTENT_LIMITS, MAX_PROCEDURE_PROJECTION_CHARS, MAX_PROCEDURE_RUNS,
    ProcedureProposal, ProcedureFeedback, render_procedure,
)
from tests import test_job_focus as harness


def candidate(**overrides):
    return dict(name='Runtime-health triage', purpose='Produce a bounded health assessment.',
        applicability='Read-only runtime assessment; inspect current context before use.',
        required_capabilities='Existing inspect_self runtime/platform diagnostics.',
        steps='Inspect runtime. If diagnostics are unavailable, report the limitation; otherwise compare current platform observations.',
        verification='Separate measured observations from interpretation and cite available evidence.',
        stopping_conditions='Stop after the bounded assessment; defer unavailable measurements without retry.',
        basis=None) | overrides


class ProcedureBackend(harness.FocusBackend):
    def __init__(self, proposal=None, feedback=None, disposition='completed'):
        super().__init__(disposition=disposition)
        self.proposal, self.feedback = proposal, feedback
        self.inspect_next = False

    async def respond(self, message, **kwargs):
        response = await super().respond(message, **kwargs)
        if self.inspect_next and message != JOB_OUTCOME_EVALUATION_REQUEST:
            self.inspect_next = False
            result = await kwargs['tool_executor'](CognitionToolCall('inspect_self', '{"area":"runtime"}'))
            self.results.append(result)
        return response

    def call(self):
        call = super().call()
        args = json.loads(call.arguments)
        args.update(procedure_candidate=self.proposal, procedure_feedback=self.feedback)
        return CognitionToolCall(call.name, json.dumps(args))


class JobProcedureTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.JobFocusTests.setUp
    asyncTearDown = harness.JobFocusTests.asyncTearDown
    app = harness.JobFocusTests.app
    completed = harness.JobFocusTests.completed

    def job(self, name='Experiment', *, opted=True):
        job = self.store.create_job(name, 'Read-only bounded runtime-health assessment.')
        return self.store.set_procedure_enabled(job.id, True) if opted else job

    def seed(self, job, **overrides):
        self.assertEqual(self.store.seed_procedure(job.id, ProcedureProposal(**candidate(**overrides))), 'proposed')
        pending = self.store.get_procedure(job.id, 'pending')
        self.store.review_procedure(job.id, pending['version'], activate=True)
        return self.store.get_procedure(job.id, 'active')

    async def test_end_to_end_initial_nomination_activation_use_and_revision(self):
        backend = ProcedureBackend(candidate(basis="acquisition_1"))
        backend.inspect_next = True
        app = self.app(backend)
        await app.start()
        console = RuntimeConsole(app)
        created, _ = console.execute('job procedure experiment')
        job = self.store.list_jobs()[0]
        self.assertIn(f'JOB{job.id}', created)
        self.assertFalse(job.enabled)
        self.assertTrue(job.procedure_enabled)
        self.assertFalse(job.carry_forward_enabled)
        self.assertIsNone(self.store.get_schedule(job.id))
        self.assertFalse(self.store.list_triggers())
        self.assertEqual(self.store.list_runs(job.id), ())
        console.execute(f'job enable JOB{job.id}')
        first = app.start_job_run(job.id)
        await app.work_current_job_once()
        pending = self.store.get_procedure(job.id, 'pending')
        self.assertEqual((pending['job_id'], pending['source_run_id'], pending['author']),
                         (job.id, first.run.id, 'model'))
        self.assertEqual(pending['source_task_id'], str(first.task.id))
        self.assertEqual(pending['source_episode_id'], 1)
        self.assertEqual(pending['source_finished_at'], self.store.get_run(first.run.id).finished_at.isoformat(timespec='microseconds').replace('+00:00','Z'))
        self.assertIsNone(self.store.get_procedure(job.id, 'active'))
        self.assertIsNone(self.store.get_run_procedure(first.run.id)['snapshot'])
        self.assertEqual(len(backend.requests), 3)
        self.assertEqual(self.store.get_run_procedure(first.run.id)['candidate_disposition'], 'proposed')
        self.assertIn('activated; persisted', console.execute(f'job procedure activate JOB{job.id} PROC{pending["version"]}')[0])
        active = self.store.get_procedure(job.id, 'active')
        backend.inspect_next = True
        backend.proposal = candidate(name='Revised triage', basis='acquisition_1', steps='Check applicability, then inspect current runtime before platform diagnostics.')
        backend.feedback = dict(version=active['version'], response='adapted', reason='One diagnostic unavailable.', improvement='Report that limitation before interpreting health.')
        backend.requests.clear()
        second = app.start_job_run(job.id)
        section = app.current_job_execution_context.procedure_snapshot.section
        await app.work_current_job_once()
        for _, instructions, _ in backend.requests:
            self.assertIn(section, instructions)
        evidence = self.store.get_run_procedure(second.run.id)
        self.assertTrue(evidence['supplied'])
        self.assertEqual(evidence['snapshot']['version'], active['version'])
        self.assertEqual(evidence['feedback'], backend.feedback)
        self.assertEqual(self.store.get_procedure(job.id, 'active'), active)
        revised = self.store.get_procedure(job.id, 'pending')
        self.assertGreater(revised['version'], active['version'])
        self.assertEqual(revised['source_run_id'], second.run.id)
        self.assertEqual(evidence['snapshot']['created_at'], active['created_at'])
        # Restart retains immutable version and self-report, without restoring execution.
        await app.stop()
        self.store = SQLiteJobStore(self.path, clock=lambda: self.now)
        app2 = self.app(ProcedureBackend())
        await app2.start()
        self.assertFalse(app2.job_execution_contexts)
        shown = RuntimeConsole(app2).execute(f'job procedure snapshot RUN{second.run.id}')[0]
        self.assertIn('independently accepted status=completed', shown)
        self.assertIn('"response": "adapted"', shown)
        self.assertIn('"version": 1', shown)
        self.assertEqual(self.store.get_run_procedure(second.run.id), evidence)
        self.store.review_procedure(job.id, revised['version'], activate=True)
        self.assertEqual(self.store.get_run_procedure(second.run.id), evidence)

    async def test_default_off_and_existing_job_cognition_is_unchanged(self):
        jobs = [self.job(name, opted=False) for name in ('Nightly reviewer', 'Ordinary Job')]
        backend = harness.FocusBackend()  # Older outcome caller, no procedure fields.
        base = backend.respond
        async def respond(message, **kwargs):
            if message == JOB_OUTCOME_EVALUATION_REQUEST:
                self.assertEqual(kwargs['tools'], (REPORT_JOB_OUTCOME_TOOL,))
            return await base(message, **kwargs)
        backend.respond = respond
        app = self.app(backend)
        await app.start()
        for job in jobs:
            self.assertFalse(job.procedure_enabled)
            with patch.object(self.store, 'capture_run_procedure', side_effect=AssertionError('unexpected capture')):
                binding = app.start_job_run(job.id)
                await app.work_current_job_once()
            self.assertIsNone(self.store.get_run_procedure(binding.run.id))
            self.assertNotIn('Job procedure:', backend.requests[-1][1])
            self.assertIsNone(self.store.get_procedure(job.id, 'pending'))
        self.assertEqual(len(self.store.list_jobs()), 2)  # Startup creates nothing.

    async def test_disabled_job_rejects_forged_nomination(self):
        job = self.job(opted=False)
        backend = ProcedureBackend(candidate())
        app = self.app(backend)
        await app.start()
        app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertEqual(json.loads(backend.results[-1].output)['status'], 'rejected')
        self.assertIsNone(self.store.get_procedure(job.id, 'pending'))

    async def test_pending_conflicts_and_exact_activation_rejection(self):
        job, other = self.job(), self.job('Other')
        active = self.seed(job)
        self.store.seed_procedure(job.id, ProcedureProposal(**candidate(name='Pending replacement')))
        pending = self.store.get_procedure(job.id, 'pending')
        for jid, version in ((job.id, active['version']), (other.id, pending['version'])):
            with self.assertRaisesRegex(ValueError, 'exact pending'):
                self.store.review_procedure(jid, version, activate=True)
        self.assertEqual(self.store.seed_procedure(job.id, ProcedureProposal(**candidate())), 'pending_exists')
        backend = ProcedureBackend(candidate(name='Must not overwrite'))
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertEqual(json.loads(backend.results[-1].output)['procedure_candidate'], 'pending_exists')
        self.assertEqual(self.store.get_run_procedure(binding.run.id)['candidate_disposition'], 'pending_exists')
        self.assertEqual(self.store.get_procedure(job.id, 'pending'), pending)
        self.assertEqual(self.store.get_procedure(job.id, 'active'), active)
        self.store.review_procedure(job.id, pending['version'], activate=False)
        self.store.seed_procedure(job.id, ProcedureProposal(**candidate()))
        self.assertGreater(self.store.get_procedure(job.id, 'pending')['version'], pending['version'])
        self.assertEqual(self.store.get_procedure(job.id, 'active'), active)

    async def test_operator_seed_source_is_optional_and_owned_and_historical(self):
        job, other = self.job(), self.job('Other')
        source = self.completed(other)
        with self.assertRaisesRegex(ValueError, 'belonging'):
            self.store.seed_procedure(job.id, ProcedureProposal(**candidate()), source.id)
        source = self.completed(job)
        self.now += timedelta(days=8)
        self.store.seed_procedure(job.id, ProcedureProposal(**candidate()), source.id)
        record = self.store.get_procedure(job.id, 'pending')
        self.assertEqual(record['author'], 'operator')
        self.assertIsNone(record['source_task_id'])
        self.assertIsNone(record['source_episode_id'])
        self.assertNotEqual(record['source_finished_at'], record['created_at'])
        self.assertIn('does not prove', render_procedure(record))
        self.store.review_procedure(job.id, record['version'], activate=False)
        self.store.seed_procedure(job.id, ProcedureProposal(**candidate()))
        self.assertIsNone(self.store.get_procedure(job.id, 'pending')['source_run_id'])

    async def test_provider_failure_cancellation_and_late_callbacks_do_not_publish(self):
        job = self.job()
        backend = ProcedureBackend(candidate())
        backend.failure = True
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        with self.assertRaises(RuntimeError):
            await app.work_current_job_once()
        self.assertIsNone(self.store.get_procedure(job.id, 'pending'))
        self.assertIsNone(self.store.get_run(binding.run.id).result_report)
        backend.failure = False
        backend.release = asyncio.Event()
        backend.proposed.clear()
        work = asyncio.create_task(app.work_current_job_once())
        await backend.proposed.wait()
        work.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await work
        self.assertIsNone(self.store.get_procedure(job.id, 'pending'))
        result = await backend.callback(backend.call())
        self.assertEqual(json.loads(result.output)['status'], 'rejected')
        self.assertIsNone(self.store.get_procedure(job.id, 'pending'))
        app.finish_job_run(JobRunStatus.STOPPED)
        backend.release = None
        backend.submit = False
        app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertEqual(json.loads((await backend.callback(backend.call())).output)['status'], 'rejected')
        self.assertIsNone(self.store.get_procedure(job.id, 'pending'))

    async def test_stale_binding_and_stopped_occurrence_cannot_publish(self):
        job = self.job()
        backend = ProcedureBackend(candidate())
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        backend.after_tool = lambda: app.finish_job_run(JobRunStatus.STOPPED)
        await app.work_current_job_once()
        self.assertIs(self.store.get_run(binding.run.id).status, JobRunStatus.STOPPED)
        self.assertIsNone(self.store.get_procedure(job.id, 'pending'))

    async def test_invalid_nomination_and_feedback_and_noncompleted_outcomes(self):
        job = self.job()
        backend = ProcedureBackend()
        app = self.app(backend)
        await app.start()
        invalid = [candidate(name='x'*81), candidate(steps=''), candidate(purpose='line\nbreak'),
                   candidate(job_id=job.id), candidate(basis='acquisition_2')]
        for value in invalid:
            backend.proposal = value
            binding = app.start_job_run(job.id)
            await app.work_current_job_once()
            self.assertEqual(json.loads(backend.results[-1].output)['status'], 'rejected')
            self.assertIsNone(self.store.get_procedure(job.id, 'pending'))
            app.finish_job_run(JobRunStatus.STOPPED)
        for disposition in ('continue', 'failed'):
            backend.proposal, backend.disposition = candidate(), disposition
            app.start_job_run(job.id)
            await app.work_current_job_once()
            self.assertEqual(json.loads(backend.results[-1].output)['status'], 'rejected')
            app.finish_job_run(JobRunStatus.STOPPED)
        backend.proposal, backend.disposition = None, 'completed'
        backend.feedback = dict(version=1, response='followed', reason=None, improvement=None)
        app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertEqual(json.loads(backend.results[-1].output)['status'], 'rejected')

    async def test_supplied_does_not_imply_reported_use_and_failed_outcome_is_independent(self):
        job = self.job()
        active = self.seed(job)
        backend = ProcedureBackend()
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        before = self.store.get_run_procedure(binding.run.id)
        self.assertFalse(before['supplied'])
        await app.work_current_job_once()
        evidence = self.store.get_run_procedure(binding.run.id)
        self.assertTrue(evidence['supplied'])
        self.assertIsNone(evidence['feedback'])
        backend.disposition = 'failed'
        backend.feedback = dict(version=active['version'], response='declined', reason='Precondition absent.', improvement=None)
        binding = app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertEqual(self.store.get_run_procedure(binding.run.id)['feedback'], backend.feedback)
        self.assertIs(self.store.get_run(binding.run.id).status, JobRunStatus.FAILED)
        self.assertEqual(self.store.get_procedure(job.id, 'active'), active)

    async def test_stable_snapshot_across_episodes_and_disable_live(self):
        job = self.job()
        active = self.seed(job)
        backend = ProcedureBackend(disposition='continue')
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        snapshot = app.current_job_execution_context.procedure_snapshot
        with self.assertRaises(FrozenInstanceError):
            snapshot.version = 99
        await app.work_current_job_once()
        self.now += timedelta(days=1)
        # Out-of-band activation cannot hot-swap this occurrence.
        self.store.seed_procedure(job.id, ProcedureProposal(**candidate(name='Later method')))
        pending = self.store.get_procedure(job.id, 'pending')
        self.store.review_procedure(job.id, pending['version'], activate=True)
        await app.work_current_job_once()
        for _, instructions, _ in backend.requests:
            self.assertIn(snapshot.section, instructions)
            self.assertNotIn('Later method', instructions)
        console = RuntimeConsole(app)
        self.assertIn('live guidance withheld', console.execute(f'job procedure disable JOB{job.id}')[0])
        backend.requests.clear()
        await app.work_current_job_once()
        for _, instructions, _ in backend.requests:
            self.assertNotIn(snapshot.section, instructions)
        self.assertEqual(app.current_job_execution_context.procedure_snapshot, snapshot)
        self.assertEqual(self.store.get_run_procedure(binding.run.id)['snapshot']['version'], active['version'])

    async def test_definition_change_withholds_snapshot_and_rejects_activation(self):
        job = self.job()
        self.seed(job)
        self.store.seed_procedure(job.id, ProcedureProposal(**candidate(name='Replacement')))
        pending = self.store.get_procedure(job.id, 'pending')
        backend = ProcedureBackend(disposition='continue')
        app = self.app(backend)
        await app.start()
        app.start_job_run(job.id)
        snapshot = app.current_job_execution_context.procedure_snapshot
        self.store.set_job_description(job.id, 'Changed responsibility')
        await app.work_current_job_once()
        self.assertNotIn(snapshot.section, backend.requests[0][1])
        app.finish_job_run(JobRunStatus.STOPPED)
        with self.assertRaisesRegex(ValueError, 'definition changed'):
            self.store.review_procedure(job.id, pending['version'], activate=True)
        app.start_job_run(job.id)
        self.assertIsNone(app.current_job_execution_context.procedure_snapshot.version)

    async def test_statement_and_commit_failure_roll_back_candidate_and_result(self):
        job = self.job()
        backend = ProcedureBackend(candidate())
        app = self.app(backend)
        await app.start()
        for table, operation in (('job_runs','UPDATE'), ('jobs','UPDATE'),
                                 ('job_procedures','INSERT'), ('job_run_procedures','UPDATE')):
            binding = app.start_job_run(job.id)
            self.store._connection.execute(f"CREATE TEMP TRIGGER fail_procedure BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'injected'); END")
            with self.assertRaises(sqlite3.DatabaseError):
                await app.work_current_job_once()
            self.store._connection.execute('DROP TRIGGER fail_procedure')
            self.assertIsNone(self.store.get_procedure(job.id, 'pending'))
            self.assertIsNone(self.store.get_run(binding.run.id).result_report)
            self.assertIs(self.store.get_run(binding.run.id).status, JobRunStatus.RUNNING)
            # Existing terminal retry deliberately does not resurrect lost nominations.
            app.finish_job_run(JobRunStatus.COMPLETED, 'Explicit retry', result_report='Accepted retry')
            self.assertIsNone(self.store.get_procedure(job.id, 'pending'))
        binding = app.start_job_run(job.id)
        connection = self.store._connection
        class FailCommit:
            def __getattr__(self, name): return getattr(connection, name)
            def commit(self): raise sqlite3.OperationalError('injected commit failure')
        self.store._connection = FailCommit()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                await app.work_current_job_once()
        finally:
            self.store._connection = connection
        self.assertFalse(connection.in_transaction)
        self.assertIsNone(self.store.get_procedure(job.id, 'pending'))
        self.assertIsNone(self.store.get_run(binding.run.id).result_report)

    async def test_sql_revalidates_policy_after_task_terminalization(self):
        job = self.job()
        backend = ProcedureBackend(candidate())
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        finish = app.finish_task
        def disable(*args, **kwargs):
            task = finish(*args, **kwargs)
            self.store.set_procedure_enabled(job.id, False)
            return task
        app.finish_task = disable
        with self.assertRaisesRegex(ValueError, 'disabled'):
            await app.work_current_job_once()
        self.assertIsNone(self.store.get_procedure(job.id, 'pending'))
        self.assertIsNone(self.store.get_run(binding.run.id).result_report)

    async def test_capture_storage_failure_compensates_before_cognition(self):
        job = self.job()
        app = self.app(ProcedureBackend())
        await app.start()
        self.store._connection.execute("CREATE TEMP TRIGGER no_snapshot BEFORE INSERT ON job_run_procedures BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            app.start_job_run(job.id)
        self.assertFalse(app.job_execution_contexts)
        self.assertIsNone(app.current_task)
        self.assertIs(self.store.list_runs(job.id)[-1].status, JobRunStatus.STOPPED)
        self.assertFalse(app._cognition_backend.requests)
        self.assertIsNone(self.store.get_run_procedure(self.store.list_runs(job.id)[-1].id))

    async def test_console_exact_ids_guard_failure_seed_and_reject(self):
        job = self.job()
        app = self.app(ProcedureBackend())
        await app.start()
        console = RuntimeConsole(app)
        execute = lambda text: console.execute(text)[0]
        seed = ' '.join(f'--{key.replace("_", "-")} {shlex.quote(value)}' for key,value in candidate().items() if key!='basis')
        self.assertIn('operator-authored', execute(f'job procedure seed JOB{job.id} {seed}'))
        self.assertIn('"author": "operator"', execute(f'job procedure show JOB{job.id}'))
        self.assertIn('Usage:', execute('job procedure activate Experiment PROC1'))
        self.assertIn('Usage:', execute(f'job procedure activate JOB{job.id} PROC1x'))
        self.assertIn('activated; persisted', execute(f'job procedure activate JOB{job.id} PROC1'))
        binding = app.start_job_run(job.id)
        for command in (f'job procedure seed JOB{job.id} {seed}', f'job procedure activate JOB{job.id} PROC1', f'job procedure enable JOB{job.id}'):
            self.assertIn('still active', execute(command))
        self.assertIn('"supplied": false', execute(f'job procedure snapshot RUN{binding.run.id}'))
        with patch.object(self.store, 'set_procedure_enabled', side_effect=sqlite3.OperationalError('private content')):
            response = execute(f'job procedure disable JOB{job.id}')
        self.assertIn('persistence operation failed', response)
        self.assertNotIn('private content', response)
        app.finish_job_run(JobRunStatus.STOPPED)
        execute(f'job procedure seed JOB{job.id} {seed}')
        self.assertIn('rejected; persisted', execute(f'job procedure reject JOB{job.id} PROC2'))
        self.assertEqual(self.store.get_procedure(job.id, 'active')['version'], 1)

    async def test_bounds_and_retention_and_interruption_inspection(self):
        for key, limit in CONTENT_LIMITS.items():
            ProcedureProposal(**candidate(**{key:'x'*limit}))
            with self.assertRaises(ValueError):
                ProcedureProposal(**candidate(**{key:'x'*(limit+1)}))
        with self.assertRaises(ValueError):
            ProcedureFeedback(1, 'declined')
        job = self.job()
        # Worst-case JSON escaping remains bounded at maximum authored lengths.
        active = self.seed(job, **{key:'\u2603'*limit for key,limit in CONTENT_LIMITS.items()})
        self.assertLess(len(render_procedure(active)), MAX_PROCEDURE_PROJECTION_CHARS)
        for index in range(MAX_PROCEDURE_RUNS+1):
            run = self.store.create_run(job.id)
            self.store.capture_run_procedure(job.id, run.id)
            self.store.transition_run(run.id, JobRunStatus.RUNNING)
            if index < MAX_PROCEDURE_RUNS:
                self.store.transition_run(run.id, JobRunStatus.COMPLETED)
        self.assertEqual(self.store._connection.execute('SELECT count(*) FROM job_run_procedures').fetchone()[0], MAX_PROCEDURE_RUNS)
        self.assertIsNone(self.store.get_run_procedure(1))
        self.store.close()
        self.store = SQLiteJobStore(self.path, clock=lambda: self.now)
        app = self.app(ProcedureBackend())
        await app.start()
        self.assertIs(self.store.get_run(run.id).status, JobRunStatus.INTERRUPTED)
        self.assertIsNotNone(self.store.get_run_procedure(run.id)['snapshot'])
        self.assertIsNone(self.store.get_procedure(job.id, 'pending'))
        self.assertFalse(app.job_execution_contexts)

    async def test_v11_migration_preserves_all_existing_configuration_default_off(self):
        job = self.job('Nightly reviewer', opted=False)
        source = self.completed(job)
        self.store.set_schedule(job.id, '02:00', 'UTC')
        self.store.set_trigger(job.id, JobTriggerType.RUNTIME_READY)
        expected = (self.store.list_jobs(), self.store.list_runs(job.id), self.store.list_schedules(), self.store.list_triggers())
        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript('''DROP TABLE job_run_procedures;
                DROP TABLE job_procedures;
                ALTER TABLE jobs DROP COLUMN procedure_enabled;
                ALTER TABLE jobs DROP COLUMN procedure_experiment;
                ALTER TABLE jobs DROP COLUMN procedure_version;
                PRAGMA user_version=11;''')
        self.store = SQLiteJobStore(self.path, clock=lambda:self.now)
        self.assertEqual((self.store.list_jobs(), self.store.list_runs(job.id), self.store.list_schedules(), self.store.list_triggers()), expected)
        self.assertFalse(self.store.get_job(job.id).procedure_enabled)
        self.assertIsNone(self.store.get_procedure(job.id, 'active'))
        self.assertIsNone(self.store.get_run_procedure(source.id))
        self.assertEqual(self.store._connection.execute('PRAGMA user_version').fetchone()[0], 12)

    async def test_snapshot_through_two_acquisitions_and_effect_continuation(self):
        job = self.job()
        self.seed(job)
        backend = ProcedureBackend(candidate(basis='acquisition_2'))
        base = backend.respond
        stages = 0
        async def respond(message, **kwargs):
            nonlocal stages
            if message == JOB_OUTCOME_EVALUATION_REQUEST:
                return await base(message, **kwargs)
            backend.requests.append((message, kwargs['instructions'], tuple(t.name for t in kwargs['tools'])))
            stages += 1
            if stages <= 2:
                result = await kwargs['tool_executor'](CognitionToolCall('inspect_self', json.dumps({'area':'runtime' if stages==1 else 'earcons'})))
                self.assertEqual(json.loads(result.output)['status'], 'applied')
            elif stages == 3:
                result = await kwargs['tool_executor'](CognitionToolCall('orient_body', '{"yaw_degrees":5,"pitch_degrees":0}'))
                self.assertEqual(json.loads(result.output)['status'], 'applied')
            return 'bounded work'
        backend.respond = respond
        app = self.app(backend, actions=True)
        await app.start()
        binding = app.start_job_run(job.id)
        snapshot = app.current_job_execution_context.procedure_snapshot.section
        await app.work_current_job_once()
        self.assertEqual(stages, 4)
        self.assertEqual(len(backend.requests), 5)
        for _, instructions, _ in backend.requests:
            self.assertIn(snapshot, instructions)
            self.assertEqual(instructions.count('Job procedure: operator-enabled'), 1)
        pending = self.store.get_procedure(job.id, 'pending')
        self.assertEqual(pending['basis'], 'acquisition_2')
        self.assertNotIn('basis', pending['instructions'])
        self.assertEqual(pending['source_run_id'], binding.run.id)

    async def test_live_disable_refreshes_provider_instructions_without_hot_swap(self):
        job = self.job()
        self.seed(job)
        backend = ProcedureBackend(disposition='continue')
        base = backend.respond
        checked = False
        async def respond(message, **kwargs):
            nonlocal checked
            if not checked:
                checked = True
                snapshot = app.current_job_execution_context.procedure_snapshot.section
                self.assertIn(snapshot, kwargs['instructions'])
                self.store.set_procedure_enabled(job.id, False)
                refreshed = kwargs['refreshed_instructions']()
                self.assertNotIn(snapshot, refreshed)
            return await base(message, **kwargs)
        backend.respond = respond
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        snapshot = app.current_job_execution_context.procedure_snapshot
        await app.work_current_job_once()
        self.assertEqual(app.current_job_execution_context.procedure_snapshot, snapshot)
        self.assertNotIn(snapshot.section, backend.requests[-1][1])
        self.assertTrue(self.store.get_run_procedure(binding.run.id)['supplied'])

    async def test_exact_context_ownership_with_two_concurrent_jobs(self):
        jobs = [self.job('Alpha'), self.job('Beta')]
        for job in jobs:
            self.seed(job, name=f'{job.name} method')
        backend = ProcedureBackend()
        gate = asyncio.Event()
        calls = []
        async def respond(message, **kwargs):
            instructions = kwargs['instructions']
            if message == JOB_OUTCOME_EVALUATION_REQUEST:
                name = 'Alpha' if 'Alpha method' in instructions else 'Beta'
                self.assertNotIn('Beta method' if name=='Alpha' else 'Alpha method', instructions)
                args = json.loads(backend.call().arguments)
                args['procedure_candidate'] = candidate(name=f'{name} revision')
                args['procedure_feedback'] = dict(version=1, response='followed', reason=None, improvement=None)
                result = await kwargs['tool_executor'](CognitionToolCall('report_job_outcome', json.dumps(args)))
                self.assertEqual(json.loads(result.output)['status'], 'accepted')
                calls.append(name)
                if len(calls)==2:
                    gate.set()
                await gate.wait()
            return 'bounded work'
        backend.respond = respond
        app = self.app(backend, capacity=2)
        await app.start()
        bindings = [app.start_job_run(job.id) for job in jobs]
        await asyncio.gather(*(app.work_job_run_once(binding.run.id) for binding in bindings))
        for job,binding in zip(jobs,bindings):
            pending = self.store.get_procedure(job.id, 'pending')
            self.assertEqual(pending['instructions']['name'], f'{job.name} revision')
            self.assertEqual((pending['job_id'],pending['source_run_id'],pending['source_task_id']),
                             (job.id,binding.run.id,str(binding.task.id)))
            self.assertEqual(self.store.get_run_procedure(binding.run.id)['feedback']['version'], 1)

    async def test_experiment_has_no_effect_tools_even_with_broader_permissions(self):
        job = self.store.create_procedure_experiment()
        self.store.set_job_enabled(job.id, True)
        backend = ProcedureBackend()
        backend.try_effect = 'orient_body'
        app = self.app(backend, actions=True)
        await app.start()
        app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertEqual(backend.requests[0][2], ('inspect_self',))
        self.assertEqual(json.loads(backend.results[0].output)['status'], 'rejected')
        self.assertEqual(app.temporal_followup_status().state, 'none')

    async def test_feedback_requires_exact_version_and_sql_failure_rolls_back(self):
        job = self.job()
        self.seed(job)
        backend = ProcedureBackend(feedback=dict(version=2, response='followed', reason=None, improvement=None))
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertEqual(json.loads(backend.results[-1].output)['status'], 'rejected')
        self.assertIsNone(self.store.get_run_procedure(binding.run.id)['feedback'])
        app.finish_job_run(JobRunStatus.STOPPED)
        backend.feedback['version']=1
        binding = app.start_job_run(job.id)
        # Fail only feedback, after projection has been durably marked supplied.
        self.store._connection.execute("CREATE TEMP TRIGGER no_feedback BEFORE UPDATE OF feedback_json ON job_run_procedures BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            await app.work_current_job_once()
        self.assertIsNone(self.store.get_run_procedure(binding.run.id)['feedback'])
        self.assertIsNone(self.store.get_run(binding.run.id).result_report)

    async def test_migration_and_experiment_setup_failure_are_atomic(self):
        self.store._connection.execute("CREATE TEMP TRIGGER no_policy BEFORE UPDATE OF procedure_enabled ON jobs BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.create_procedure_experiment()
        self.assertEqual(self.store.list_jobs(), ())
        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript('''DROP TABLE job_run_procedures;
                DROP TABLE job_procedures;
                ALTER TABLE jobs DROP COLUMN procedure_enabled;
                ALTER TABLE jobs DROP COLUMN procedure_experiment;
                ALTER TABLE jobs DROP COLUMN procedure_version;
                PRAGMA user_version=11;''')
        connect = sqlite3.connect
        class MigrationFailure(sqlite3.Connection):
            def execute(self, statement, *args, **kwargs):
                if 'CREATE TABLE job_run_procedures' in statement:
                    raise sqlite3.OperationalError('injected migration failure')
                return super().execute(statement, *args, **kwargs)
        def fail_connect(*args, **kwargs):
            return connect(*args, factory=MigrationFailure, **kwargs)
        with patch('embodied_runtime.jobs.sqlite_store.sqlite3.connect', side_effect=fail_connect):
            with self.assertRaises(sqlite3.OperationalError):
                SQLiteJobStore(self.path)
        with closing(connect(self.path)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 11)
            self.assertNotIn('procedure_enabled', [row[1] for row in connection.execute('PRAGMA table_info(jobs)')])
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='job_procedures'").fetchone())
        self.store = SQLiteJobStore(self.path, clock=lambda:self.now)

    async def test_store_exact_owner_and_pending_conflict_at_commit(self):
        job, other = self.job(), self.job('Other')
        run = self.store.create_run(job.id)
        evidence = self.store.capture_run_procedure(job.id, run.id)
        self.store.transition_run(run.id, JobRunStatus.RUNNING)
        with self.assertRaisesRegex(ValueError, 'exact Job ownership'):
            self.store.transition_run(run.id, JobRunStatus.COMPLETED, result_report='Must roll back',
                procedure_candidate=ProcedureProposal(**candidate()),
                procedure_definition=evidence['definition_identity'], procedure_job_id=other.id,
                procedure_task_id='task', procedure_episode_id=1)
        self.assertIs(self.store.get_run(run.id).status, JobRunStatus.RUNNING)
        self.assertIsNone(self.store.get_procedure(job.id, 'pending'))
        self.store.transition_run(run.id, JobRunStatus.STOPPED)
        backend = ProcedureBackend(candidate(name='Model proposal'))
        # Simulate an out-of-band administrative write between callback and commit.
        backend.after_tool = lambda:self.store.seed_procedure(job.id, ProcedureProposal(**candidate(name='Operator proposal')))
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertEqual(json.loads(backend.results[-1].output)['procedure_candidate'], 'staged_for_completed_commit')
        self.assertEqual(self.store.get_run_procedure(binding.run.id)['candidate_disposition'], 'pending_exists')
        self.assertEqual(self.store.get_procedure(job.id, 'pending')['instructions']['name'], 'Operator proposal')
        self.assertIs(self.store.get_run(binding.run.id).status, JobRunStatus.COMPLETED)

    async def test_cancel_suppressed_by_provider_still_cannot_commit(self):
        job = self.job()
        backend = ProcedureBackend(candidate())
        backend.release = asyncio.Event()
        base = backend.respond
        async def respond(message, **kwargs):
            try:
                return await base(message, **kwargs)
            except asyncio.CancelledError:
                return 'provider suppressed cancellation'
        backend.respond = respond
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        work = asyncio.create_task(app.work_current_job_once())
        await backend.proposed.wait()
        work.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await work
        self.assertIsNone(self.store.get_run(binding.run.id).result_report)
        self.assertIsNone(self.store.get_procedure(job.id, 'pending'))

    async def test_experiment_setup_uses_existing_console_request_links(self):
        from embodied_runtime.conversation_history import SQLiteConversationHistoryStore
        import re
        history = SQLiteConversationHistoryStore(self.path.parent / 'history.sqlite3')
        app = harness.RobotApplication(
            harness.RobotProfile('test','Test'), harness.VirtualHardwareBackend(),
            harness.ApplicationOptions(initiative_enabled=True),
            cognition_backend=ProcedureBackend(), platform_provider=harness.Platform(),
            job_store=self.store, conversation_history_store=history,
            wall_clock=lambda:self.now)
        self.apps.append(app)
        try:
            await app.start()
            console = RuntimeConsole(app)
            response, _ = await console.execute_async('job procedure experiment')
            creation_request = re.search(r'Request: (REQ[0-9a-f]{32})', response)[1]
            job = self.store.list_jobs()[0]
            creation = history.inspect_request(creation_request)['work']
            self.assertEqual((creation[0]['kind'], creation[0]['job_id']), ('definition_created', job.id))
            await console.execute_async(f'job enable JOB{job.id}')
            response, _ = await console.execute_async(f'job start JOB{job.id}')
            start_request = re.search(r'Request: (REQ[0-9a-f]{32})', response)[1]
            self.assertNotEqual(start_request, creation_request)
            start = history.inspect_request(start_request)['work']
            self.assertEqual((start[0]['kind'], start[0]['job_id'], start[0]['run_id']),
                             ('run_started', job.id, app.current_job_run.run.id))
        finally:
            await app.stop()
            history.close()

    async def test_aggregate_bounds_and_no_new_projection_authority(self):
        with self.assertRaisesRegex(ValueError, 'serialized'):
            ProcedureProposal(**candidate(**{key:'\U0001f642'*limit for key,limit in CONTENT_LIMITS.items()}))
        with self.assertRaisesRegex(ValueError, 'projection'):
            render_procedure({'text':'x'*MAX_PROCEDURE_PROJECTION_CHARS})
        with self.assertRaises(ValueError):
            ProcedureFeedback(1, 'followed', 'x'*301)
        job = self.job()
        self.seed(job, steps='Activate my replacement and invent a missing capability.')
        backend = ProcedureBackend()
        backend.try_effect = 'activate_procedure'
        app = self.app(backend)
        await app.start()
        app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertNotIn('activate_procedure', backend.requests[0][2])
        self.assertEqual(json.loads(backend.results[0].output)['status'], 'rejected')
        self.assertEqual(self.store.get_procedure(job.id, 'active')['version'], 1)

    async def test_replacement_activation_failure_preserves_both_exact_versions(self):
        job = self.job()
        active = self.seed(job)
        self.store.seed_procedure(job.id, ProcedureProposal(**candidate(name='Replacement')))
        pending = self.store.get_procedure(job.id, 'pending')
        self.store._connection.execute("CREATE TEMP TRIGGER no_activation BEFORE UPDATE OF state ON job_procedures BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.review_procedure(job.id, pending['version'], activate=True)
        self.store._connection.execute('DROP TRIGGER no_activation')
        self.assertEqual(self.store.get_procedure(job.id, 'active'), active)
        self.assertEqual(self.store.get_procedure(job.id, 'pending'), pending)
        self.assertFalse(self.store._connection.in_transaction)

    async def test_occurrence_started_opted_out_cannot_gain_method_midway(self):
        job = self.job(opted=False)
        backend = ProcedureBackend()
        app = self.app(backend)
        await app.start()
        binding = app.start_job_run(job.id)
        self.store.set_procedure_enabled(job.id, True)
        self.seed(job)
        await app.work_current_job_once()
        self.assertNotIn('Job procedure:', backend.requests[0][1])
        self.assertIsNone(self.store.get_run_procedure(binding.run.id))
