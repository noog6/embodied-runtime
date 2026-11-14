"""Offline paired trials use production Job cognition, snapshots and SQLite."""

import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import json
import re
import sqlite3
import unittest
from unittest.mock import patch

from embodied_runtime.app import JOB_OUTCOME_EVALUATION_REQUEST
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.console import RuntimeConsole
from embodied_runtime.jobs import JobRunStatus, SQLiteJobStore
from embodied_runtime.jobs.procedure import ProcedureProposal
from embodied_runtime.jobs.trial import trial_configuration, CASES, score_attempt
from tests.test_job_procedures import ProcedureBackend, candidate
from tests import test_job_focus as harness
from tests.test_interaction import RecordingSink


class DiagnosticBackend(TextCognitionBackend):
    identifier = 'diagnostic-test'
    model = 'fixed-offline-model'

    def __init__(self, *, regression=False, equal=False, gate=None, malicious=False, usage=False):
        self.regression, self.equal = regression, equal
        self.gate, self.malicious, self.usage = gate, malicious, usage
        self.facts = {}
        self.inspected = False
        self.results = []
        self.observability = None

    async def respond(self, message, *, instructions=None, tools=(), tool_executor=None,
                      refreshed_instructions=None, **kwargs):
        if self.gate:
            self.gate[0].set()
            await self.gate[1].wait()
        if self.usage:
            self.observability.provider_completed(self.identifier, self.model, 'trial',
                input_tokens=100, output_tokens=20, total_tokens=120)
        version = int(re.search(r'"version": (\d+)', instructions).group(1))
        pending = version > 1 and not self.equal
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            decline = 'Diagnostic case scope: physical_sensor_calibration' in instructions
            state = self.facts.get('diagnostic_state')
            assessment = {'normal': 'bounded_normal', 'unavailable': 'unknown',
                          'memory_pressure': 'memory_pressure', 'not_applicable': 'not_applicable'}[state]
            best = {'normal': 'physical_sensor_reading', 'unavailable': 'runtime_diagnostic_sample',
                    'memory_pressure': 'memory_sample', 'not_applicable': 'operator_scope_clarification'}[state]
            useful = {'normal': 'network_probe', 'unavailable': 'physical_sensor_reading',
                      'memory_pressure': 'runtime_diagnostic_sample', 'not_applicable': 'physical_sensor_reading'}[state]
            report = dict(assessment='overall_healthy' if self.regression and pending else assessment,
                          evidence=self.facts,
                          limitation='outside_scope' if decline else 'diagnostics_missing' if state == 'unavailable' else 'physical_health_unverified',
                          next_diagnostic=best if pending else useful,
                          applicability='decline' if decline else 'apply')
            args = dict(disposition='completed', summary='Synthetic diagnostic comparison', report=json.dumps(report),
                        readiness=None, delay_seconds=None, procedure_feedback=None)
            if self.malicious:
                args['procedure_candidate'] = candidate()
            self.results.append(await tool_executor(CognitionToolCall('report_job_outcome', json.dumps(args))))
        elif not self.inspected:
            self.inspected = True
            if self.malicious:
                for name in ('workspace_write', 'address_operator', 'express', 'publish_finding'):
                    self.results.append(await tool_executor(CognitionToolCall(name, '{}')))
            result = await tool_executor(CognitionToolCall('inspect_self', '{"area":"runtime"}'))
            self.results.append(result)
            self.facts = {item['name']: item['value'] for item in json.loads(result.output)['facts']}
        return 'Bounded diagnostic work.'


class RequestBackend(ProcedureBackend):
    identifier = 'diagnostic-test'
    model = 'fixed-offline-model'

    def __init__(self, **trial_options):
        super().__init__()
        self.versions = {'active': 1, 'pending': 2}
        self.trial_options = trial_options
        self.forks = []

    def call(self):
        call = super().call()
        return CognitionToolCall(call.name, json.dumps(json.loads(call.arguments) | {'procedure_trial': self.versions}))

    def fork_procedure_trial(self):
        backend = DiagnosticBackend(**self.trial_options)
        self.forks.append(backend)
        return backend


class ProcedureTrialTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.JobFocusTests.setUp
    asyncTearDown = harness.JobFocusTests.asyncTearDown
    app = harness.JobFocusTests.app

    async def setup_trial(self, backend=None, *, actions=False, scheduled=False):
        backend = backend or RequestBackend()
        # Catalog identities are deliberately not JOB19; versions come from the store.
        self.store.create_job('Unrelated existing Job')
        job = self.store.create_procedure_experiment()
        job = self.store.set_job_enabled(job.id, True)
        if scheduled:
            self.store.set_schedule(job.id, '00:00', 'UTC')
        for name in ('Incumbent', 'Candidate'):
            self.store.seed_procedure(job.id, ProcedureProposal(**candidate(name=name)))
            if name == 'Incumbent':
                self.store.review_procedure(job.id, self.store.get_procedure(job.id, 'pending')['version'], activate=True)
        app = self.app(backend, actions=actions)
        if scheduled:
            with patch.object(app.scheduled_job_controller, 'start'):
                await app.start()
        else:
            await app.start()
        binding = app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertEqual(self.store.get_run(binding.run.id).status, JobRunStatus.COMPLETED)
        return app, job, backend, self.store.list_procedure_trials(job.id)[0]

    async def finish(self, app, trial_id):
        for _ in range(20):
            await app.advance_procedure_trials_once()
            trial = self.store.get_procedure_trial(trial_id)
            if trial['state'] not in ('running', 'queued'):
                return trial
        self.fail('bounded trial did not settle')

    async def test_winner_inspection_approval_fresh_run_and_recoverable_incumbent(self):
        app, job, backend, trial = await self.setup_trial(RequestBackend(usage=True))
        active = self.store.get_procedure(job.id, 'active')
        result = await self.finish(app, trial['id'])
        self.assertEqual(result['report']['recommendation'], 'candidate')
        self.assertEqual(self.store.get_procedure(job.id, 'active'), active)
        attempts = self.store.procedure_trial_attempts(trial['id'])
        self.assertEqual(len(attempts), 12)
        self.assertEqual(len({a['run_id'] for a in attempts}), 12)
        for index in range(0, 12, 2):
            pair = attempts[index:index+2]
            self.assertEqual(pair[0]['result']['input_identity'], pair[1]['result']['input_identity'])
            self.assertEqual([a['procedure_version'] for a in pair], [1, 2])
            self.assertTrue(all(a['result']['procedure_evidence']['supplied'] for a in pair))
            self.assertTrue(all(a['result']['provider_calls'] for a in pair))
        console = RuntimeConsole(app)
        shown = console.execute(f'job procedure trial TRIAL{trial["id"]}')[0]
        self.assertIn('candidate', shown)
        self.assertIn('current_evidence', shown)
        self.assertIn('TRIAL', console.execute(f'job procedure trials JOB{job.id}')[0])
        self.assertIn('activated; persisted', console.execute(f'job procedure activate JOB{job.id} PROC2')[0])
        promoted = self.store.get_procedure_trial(trial['id'])
        self.assertEqual(promoted['promotion']['previous'], active)
        self.assertEqual(promoted['promotion']['approval'], 'explicit_console')
        ordinary = app.start_job_run(job.id)
        self.assertEqual(self.store.get_run_procedure(ordinary.run.id)['snapshot']['version'], 2)
        app.finish_job_run(JobRunStatus.STOPPED, 'offline check complete')
        # Restore exact authored content through the existing seed/activation path.
        old = promoted['promotion']['previous']
        self.store.seed_procedure(job.id, ProcedureProposal(**old['instructions']))
        self.store.review_procedure(job.id, 3, activate=True)
        self.assertEqual(self.store.get_procedure(job.id, 'active')['instructions'], old['instructions'])

    async def test_trial_pending_projection_and_simultaneous_ordinary_and_scheduled_admission(self):
        entered, release = asyncio.Event(), asyncio.Event()
        app, job, backend, trial = await self.setup_trial(RequestBackend(gate=(entered, release)), actions=True)
        task = asyncio.create_task(app.advance_procedure_trials_once())
        await entered.wait()
        ordinary = app.start_job_run(job.id)
        self.assertEqual(self.store.get_run_procedure(ordinary.run.id)['snapshot']['version'], 1)
        with self.assertRaisesRegex(RuntimeError, 'active occurrence'):
            app.start_job_run(job.id)
        app.finish_job_run(JobRunStatus.STOPPED, 'completed concurrency check')
        # Store schedule admission has the same ordinary single-occurrence rule.
        schedule = self.store.set_schedule(job.id, '00:00', 'UTC')
        scheduled = self.store.create_scheduled_run(job.id, '2026-10-01', expected_schedule=schedule)
        self.assertIsNotNone(scheduled)
        self.store.transition_run(scheduled.id, JobRunStatus.STOPPED)
        release.set()
        await task
        self.assertTrue(app.options.initiative_actions_enabled)
        self.assertIsNone(self.store.get_procedure_trial(trial['id'])['promotion'])

    async def test_regression_and_equal_results_never_recommend_activation(self):
        for options, recommendation in (({'regression': True}, 'inconclusive'), ({'equal': True}, 'incumbent')):
            with self.subTest(options=options):
                app, job, backend, trial = await self.setup_trial(RequestBackend(**options))
                result = await self.finish(app, trial['id'])
                self.assertEqual(result['report']['recommendation'], recommendation)
                self.assertEqual(result['notification'], 'unclaimed')
                with self.assertRaisesRegex(ValueError, 'trial recommendation'):
                    self.store.review_procedure(job.id, 2, activate=True)
                self.assertEqual(self.store.get_procedure(job.id, 'active')['version'], 1)

    async def test_normal_scheduler_captures_incumbent_while_pending_trial_is_running(self):
        entered, release = asyncio.Event(), asyncio.Event()
        app, job, backend, trial = await self.setup_trial(RequestBackend(gate=(entered, release)), scheduled=True)
        task = asyncio.create_task(app.advance_procedure_trials_once())
        await entered.wait()
        try:
            await app._offer_scheduled_job()
            context = app.current_job_execution_context
            self.assertIsNotNone(context)
            self.assertEqual(context.procedure_snapshot.version, 1)
            self.assertNotEqual(context.run_id, self.store.procedure_trial_attempts(trial['id'])[0]['run_id'])
            await context.active_work_task
        finally:
            release.set()
            await task

    async def test_restart_keeps_finished_and_interrupted_attempts_and_charges_replacement(self):
        app, job, backend, trial = await self.setup_trial()
        await app.advance_procedure_trials_once()
        first = self.store.procedure_trial_attempts(trial['id'])[0]
        unfinished = self.store.begin_procedure_trial_attempt(trial['id'], trial_configuration(app))
        self.store.transition_run(unfinished['run_id'], JobRunStatus.RUNNING)
        await app.stop()
        self.store = SQLiteJobStore(self.path, clock=lambda: self.now)
        app2 = self.app(RequestBackend())
        await app2.start()
        self.assertEqual(self.store.get_run(unfinished['run_id']).status, JobRunStatus.INTERRUPTED)
        result = await self.finish(app2, trial['id'])
        attempts = self.store.procedure_trial_attempts(trial['id'])
        self.assertEqual(attempts[0], first)
        self.assertEqual(attempts[1]['state'], 'interrupted')
        self.assertEqual(attempts[1]['case_id'], attempts[2]['case_id'])
        self.assertNotEqual(attempts[1]['run_id'], attempts[2]['run_id'])
        self.assertEqual(len(attempts), 13)
        self.assertEqual(result['report']['recommendation'], 'candidate')
        await app2.stop()
        self.store = SQLiteJobStore(self.path, clock=lambda: self.now)
        self.assertEqual(self.store.get_procedure_trial(trial['id']), result)

    async def test_changed_definition_candidate_configuration_and_incumbent_stale_recommendation(self):
        app, job, backend, trial = await self.setup_trial()
        result = await self.finish(app, trial['id'])
        self.assertTrue(self.store.procedure_trial_current(result))
        self.store.set_job_description(job.id, 'Changed responsibility')
        self.assertFalse(self.store.procedure_trial_current(result))
        with self.assertRaises(ValueError):
            self.store.review_procedure(job.id, 2, activate=True)
        self.store.set_job_description(job.id, job.description)
        self.store.review_procedure(job.id, 2, activate=False)
        self.store.seed_procedure(job.id, ProcedureProposal(**candidate(name='Replaced')))
        self.assertFalse(self.store.procedure_trial_current(result))
        # An untouched pair with changed model is abandoned before spending any attempt.
        app2, job2, backend2, trial2 = await self.setup_trial()
        backend2.model = 'different-model'
        await app2.advance_procedure_trials_once()
        self.assertEqual(self.store.get_procedure_trial(trial2['id'])['state'], 'stale')
        self.assertEqual(self.store.procedure_trial_attempts(trial2['id']), ())

    async def test_trial_effects_and_nominations_rejected_even_with_production_actions(self):
        app, job, backend, trial = await self.setup_trial(RequestBackend(malicious=True), actions=True)
        await app.advance_procedure_trials_once()
        statuses = [json.loads(r.output)['status'] for r in backend.forks[0].results]
        self.assertEqual(statuses[:4], ['rejected'] * 4)
        self.assertEqual(statuses[-1], 'rejected')
        self.assertEqual(self.store.get_procedure(job.id, 'pending')['version'], 2)
        self.assertEqual(self.store.get_procedure(job.id, 'active')['version'], 1)
        self.assertFalse(self.store.procedure_trial_attempts(trial['id'])[0]['result']['score']['valid'])

    async def test_request_scope_exact_ids_provider_failure_and_idempotency(self):
        app, job, backend, trial = await self.setup_trial()
        app.start_job_run(job.id)
        await app.work_current_job_once()
        self.assertEqual(len(self.store.list_procedure_trials(job.id)), 1)
        for versions in ({'active': 1, 'pending': 99}, {'active': True, 'pending': 2}):
            with self.assertRaises(ValueError):
                self.store.validate_procedure_trial_request(job.id, versions)
        other = self.store.create_job('Not an experiment')
        self.store.set_procedure_enabled(other.id, True)
        with self.assertRaises(ValueError):
            self.store.validate_procedure_trial_request(other.id, {'active': 1, 'pending': 2})
        # A staged request publishes nothing if the provider fails after callback.
        self.store.review_procedure(job.id, 2, activate=False)
        self.store.seed_procedure(job.id, ProcedureProposal(**candidate(name='New pending')))
        backend.versions = {'active': 1, 'pending': 3}
        backend.failure = True
        app.start_job_run(job.id)
        with self.assertRaises(Exception):
            await app.work_current_job_once()
        self.assertEqual(len(self.store.list_procedure_trials(job.id)), 1)

    async def test_attempt_budget_exhaustion_stays_inspectable_and_inconclusive(self):
        app, job, backend, trial = await self.setup_trial()
        for _ in range(trial['configuration']['budget']['attempts']):
            attempt = self.store.begin_procedure_trial_attempt(trial['id'], trial_configuration(app))
            self.store.transition_run(attempt['run_id'], JobRunStatus.INTERRUPTED)
        await app.advance_procedure_trials_once()
        result = self.store.get_procedure_trial(trial['id'])
        self.assertEqual(result['report']['recommendation'], 'inconclusive')
        self.assertEqual(len(self.store.procedure_trial_attempts(trial['id'])), 14)

    async def test_terminal_request_and_trial_import_storage_failure_are_atomic(self):
        app, job, backend, trial = await self.setup_trial()
        self.store._connection.execute("""CREATE TEMP TRIGGER block_import BEFORE UPDATE ON procedure_trial_attempts
            WHEN json_extract(NEW.record_json,'$.state')='finished'
            BEGIN SELECT RAISE(ABORT,'injected'); END""")
        with self.assertRaises(sqlite3.DatabaseError):
            await app.advance_procedure_trials_once()
        attempt = self.store.procedure_trial_attempts(trial['id'])[0]
        self.assertEqual(attempt['state'], 'interrupted')
        self.assertIsNone(self.store.get_run(attempt['run_id']).result_report)
        self.assertIsNone(self.store.get_run_procedure(attempt['run_id']))
        self.assertEqual(self.store.get_run(attempt['run_id']).status, JobRunStatus.INTERRUPTED)

    async def test_notification_success_failure_and_restart_never_repeat_delivery(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                app, job, backend, trial = await self.setup_trial()
                sink = RecordingSink(fail=failure)
                app.options = replace(app.options, initiative_messages_enabled=True)
                app._operator_message_sink = sink
                result = await self.finish(app, trial['id'])
                self.assertEqual(result['notification'], 'rejected' if failure else 'applied')
                if not failure:
                    self.assertEqual(len(sink.messages), 1)
                    self.assertIn(f'job procedure activate JOB{job.id} PROC2', sink.messages[0].text)
                    self.assertIn(f'TRIAL{trial["id"]}', sink.messages[0].text)
                    self.assertFalse(sink.messages[0].interaction.response_expected)
                self.assertEqual(self.store.get_procedure(job.id, 'active')['version'], 1)
                await app.advance_procedure_trials_once()
                self.assertEqual(len(sink.messages), 0 if failure else 1)
                await app.stop()
                self.store = SQLiteJobStore(self.path, clock=lambda: self.now)
                app2 = self.app(RequestBackend())
                app2.options = replace(app2.options, initiative_messages_enabled=True)
                app2._operator_message_sink = sink
                await app2.start()
                sink.fail = False
                await app2.advance_procedure_trials_once()
                self.assertEqual(len(sink.messages), 0 if failure else 1)
                self.assertEqual(self.store.get_procedure_trial(trial['id']), result)

    async def test_activation_is_atomic_and_requires_enabled_idle_job_and_exact_incumbent(self):
        app, job, backend, trial = await self.setup_trial()
        await self.finish(app, trial['id'])
        self.store.set_job_enabled(job.id, False)
        with self.assertRaises(ValueError):
            self.store.review_procedure(job.id, 2, activate=True)
        self.store.set_job_enabled(job.id, True)
        ordinary = app.start_job_run(job.id)
        with self.assertRaises(ValueError):
            self.store.review_procedure(job.id, 2, activate=True)
        app.finish_job_run(JobRunStatus.STOPPED, 'idle again')
        active = self.store.get_procedure(job.id, 'active')
        changed = dict(active, instructions=dict(active['instructions'], steps='Changed incumbent text'))
        self.store._connection.execute("UPDATE job_procedures SET record_json=? WHERE job_id=? AND state='active'",
                                       (json.dumps(changed), job.id))
        with self.assertRaises(ValueError):
            self.store.review_procedure(job.id, 2, activate=True)
        self.store._connection.execute("UPDATE job_procedures SET record_json=? WHERE job_id=? AND state='active'",
                                       (json.dumps(active), job.id))
        self.store._connection.execute("CREATE TEMP TRIGGER block_promotion BEFORE UPDATE ON job_procedures BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.review_procedure(job.id, 2, activate=True)
        self.assertEqual(self.store.get_procedure(job.id, 'active'), active)
        self.assertIsNone(self.store.get_procedure_trial(trial['id'])['promotion'])

    async def test_request_persistence_failure_rolls_back_source_completion_and_trial(self):
        app, job, backend, trial = await self.setup_trial()
        self.store.review_procedure(job.id, 2, activate=False)
        self.store.seed_procedure(job.id, ProcedureProposal(**candidate(name='Next candidate')))
        backend.versions = {'active': 1, 'pending': 3}
        self.store._connection.execute("CREATE TEMP TRIGGER block_trial BEFORE INSERT ON procedure_trials BEGIN SELECT RAISE(ABORT,'injected'); END")
        run = app.start_job_run(job.id)
        with self.assertRaises(sqlite3.DatabaseError):
            await app.work_current_job_once()
        self.assertEqual(len(self.store.list_procedure_trials(job.id)), 1)
        self.assertEqual(self.store.get_run(run.run.id).status, JobRunStatus.RUNNING)
        self.assertIsNone(self.store.get_run(run.run.id).result_report)

    async def test_shutdown_cancels_owned_trial_and_preserves_interrupted_attempt(self):
        entered, release = asyncio.Event(), asyncio.Event()
        app, job, backend, trial = await self.setup_trial(RequestBackend(gate=(entered, release)))
        app._procedure_trial_task.cancel()
        await asyncio.gather(app._procedure_trial_task, return_exceptions=True)
        app._procedure_trial_task = asyncio.create_task(app.advance_procedure_trials_once())
        await entered.wait()
        run_id = self.store.procedure_trial_attempts(trial['id'])[0]['run_id']
        await app.stop()
        self.store = SQLiteJobStore(self.path, clock=lambda: self.now)
        self.assertEqual(self.store.get_run(run_id).status, JobRunStatus.INTERRUPTED)
        self.assertEqual(self.store.procedure_trial_attempts(trial['id'])[0]['state'], 'interrupted')
        self.assertIsNone(self.store.get_run(run_id).result_report)
        app2 = self.app(RequestBackend())
        await app2.start()
        result = await self.finish(app2, trial['id'])
        self.assertEqual(result['report']['recommendation'], 'candidate')
        self.assertEqual(len(self.store.procedure_trial_attempts(trial['id'])), 13)

    async def test_schema_12_migration_preserves_catalog_and_rolls_back_failure(self):
        job = self.store.create_procedure_experiment()
        self.store.seed_procedure(job.id, ProcedureProposal(**candidate()))
        expected = self.store.get_procedure(job.id, 'pending')
        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript('''DROP TABLE procedure_trial_attempts;
                DROP TABLE procedure_trials; PRAGMA user_version=12;''')
        connect = sqlite3.connect
        class BrokenMigration(sqlite3.Connection):
            def execute(self, statement, *args, **kwargs):
                if 'CREATE TABLE procedure_trial_attempts' in statement:
                    raise sqlite3.OperationalError('injected')
                return super().execute(statement, *args, **kwargs)
        with patch('embodied_runtime.jobs.sqlite_store.sqlite3.connect',
                   side_effect=lambda *a, **kw: connect(*a, factory=BrokenMigration, **kw)):
            with self.assertRaises(sqlite3.OperationalError):
                SQLiteJobStore(self.path)
        with closing(connect(self.path)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 12)
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='procedure_trials'").fetchone())
        self.store = SQLiteJobStore(self.path, clock=lambda: self.now)
        self.assertEqual(self.store.get_job(job.id), job)
        self.assertEqual(self.store.get_procedure(job.id, 'pending'), expected)
        self.assertEqual(self.store.list_procedure_trials(), ())

    async def test_comparison_survives_ordinary_procedure_snapshot_retention(self):
        from embodied_runtime.jobs.procedure import MAX_PROCEDURE_RUNS
        app, job, backend, trial = await self.setup_trial()
        result = await self.finish(app, trial['id'])
        attempts = self.store.procedure_trial_attempts(trial['id'])
        for _ in range(MAX_PROCEDURE_RUNS):
            app.start_job_run(job.id)
            app.finish_job_run(JobRunStatus.STOPPED, 'Bounded retention check')
        self.assertIsNone(self.store.get_run_procedure(attempts[0]['run_id']))
        self.assertEqual(self.store.get_procedure_trial(trial['id']), result)
        self.assertEqual(self.store.procedure_trial_attempts(trial['id']), attempts)
        count = self.store._connection.execute('SELECT count(*) FROM job_run_procedures WHERE job_id=?', (job.id,)).fetchone()[0]
        self.assertEqual(count, MAX_PROCEDURE_RUNS)

    async def test_synthetic_runs_do_not_become_ordinary_activity_or_latest_result(self):
        app, job, backend, trial = await self.setup_trial()
        source = self.store.get_latest_completed_run(job.id)
        await self.finish(app, trial['id'])
        self.assertEqual(self.store.get_latest_completed_run(job.id), source)
        activity = self.store.summarize_activity(self.now, self.now + timedelta(days=1),
                                               retrieved_at=self.now + timedelta(days=2))
        self.assertEqual(activity['totals']['records'], 1)
        self.assertEqual(activity['excluded_record_kinds'], ['procedure_trial'])
        self.assertEqual([r['run_id'] for r in activity['runs']], [source.id])
        self.assertEqual(len(self.store.list_runs(job.id)), 13)  # All trial IDs remain inspectable.


class TrialScoringTests(unittest.TestCase):
    def test_authoritative_scope_can_support_declining_without_irrelevant_acquisition(self):
        report = dict(assessment='not_applicable', evidence={}, limitation='outside_scope',
                      next_diagnostic='operator_scope_clarification', applicability='decline')
        score = score_attempt(CASES[3], json.dumps(report), [], 'completed')
        self.assertTrue(score['valid'])
        self.assertEqual(score['total'], 5)

    def test_self_report_or_stale_claim_is_not_current_evidence(self):
        case = CASES[2]
        report = dict(assessment='memory_pressure', evidence={'diagnostic_state': 'normal'},
                      limitation='physical_health_unverified', next_diagnostic='memory_sample', applicability='apply')
        score = score_attempt(case, json.dumps(report), [], 'completed')
        self.assertFalse(score['valid'])
        self.assertIn('unsupported or stale evidence treated as current', score['regressions'])

    def test_gain_cannot_compensate_a_protected_criterion_regression(self):
        from embodied_runtime.jobs.trial import compare, RUBRIC
        attempts = []
        for case in CASES:
            for variant in ('active', 'pending'):
                scores = dict(current_evidence=2, next_diagnostic=1 if variant == 'active' else 2, applicability=1)
                if case['id'] == 'normal' and variant == 'pending':
                    scores['current_evidence'] = 1
                attempts.append(dict(run_id=len(attempts)+1, case_id=case['id'], variant=variant,
                                     state='finished', result=dict(score=dict(valid=True, scores=scores,
                                     total=sum(scores.values()), regressions=[]))))
        report = compare(dict(cases=CASES, rubric=RUBRIC), attempts)
        self.assertEqual(report['recommendation'], 'incumbent')
        self.assertIn('normal: protected current_evidence score decreased', report['regressions'])

