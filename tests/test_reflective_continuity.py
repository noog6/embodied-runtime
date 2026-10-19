"""Scripted reflection examples through existing tools, never a production reflection engine."""
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shlex
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from embodied_runtime.app import ApplicationOptions, JOB_OUTCOME_EVALUATION_REQUEST, RobotApplication
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.cli import parse_launch_arguments
from embodied_runtime.console import RuntimeConsole
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    FilesystemJobWorkspaceStore, JobRunStatus, SQLiteJobStore, WorkspaceBackendError,
)
from embodied_runtime.profile import RobotProfile
from tests.test_job_execution import Platform

ROOT = Path(__file__).resolve().parents[1]
DEFINITION = tomllib.loads((ROOT / 'config/experiments/reflective-continuity-job.toml').read_text())
EXAMPLES = json.loads((ROOT / 'tests/fixtures/reflective_continuity.json').read_text())


def token(reference):
    return (reference['job'], reference.get('run'), reference.get('path'), reference.get('version'))


class ScriptedReflection(TextCognitionBackend):
    """Replay explicitly authored examples; semantics are supplied, not inferred by this fake.

    A fresh instance is used each Run. Earlier understanding/references come only
    from the actual own-Workspace read, not a hidden provider conversation.
    """
    identifier = 'scripted-reflection-fixture'

    def __init__(self, plan, example, now):
        self.plan, self.example, self.now = plan, example, now
        self.calls, self.tools = [], []
        self.previous = self.source = self.saved = None
        self.restore_error = None
        self.saved_content = None
        self.write_confirmed = False

    async def call(self, execute, name, arguments):
        result = json.loads((await execute(CognitionToolCall(name, json.dumps(arguments)))).output)
        self.calls.append((name, arguments, result))
        return result

    async def respond(self, message, *, tools=(), tool_executor=None, **kwargs):
        self.tools.append({tool.name for tool in tools})
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            await self.call(tool_executor, 'report_job_outcome', {
                'disposition': 'completed' if self.write_confirmed else 'failed',
                'summary': 'Reflection account saved' if self.write_confirmed else 'Reflection not confirmed saved',
                'report': self.saved_content if self.write_confirmed else self.restore_error or 'Workspace write not confirmed',
                'readiness': None, 'delay_seconds': None,
            })
            return 'outcome'
        if not self.calls:
            result = await self.call(tool_executor, 'workspace_read', {'path': 'reflection.json', 'offset_chars': 0})
            if result.get('reason') == 'artifact_not_found':
                self.previous = dict(account='', questions=[], retired=[], reviewed={}, next=0)
            else:
                try:
                    if (result['status'] != 'ok' or result['artifact']['truncated']
                            or len(result['artifact']['content']) > 4000):
                        raise ValueError('previous account unavailable or partial')
                    self.previous = json.loads(result['artifact']['content'])
                    if (not 0 <= self.previous['next'] < len(self.plan)
                            or len(self.previous['questions']) > 3):
                        raise ValueError('invalid previous account')
                except (KeyError, TypeError, ValueError):
                    self.restore_error = 'Previous account unavailable, invalid or truncated; not overwritten.'
            return 'state read'
        if self.restore_error:
            return self.restore_error
        if self.source is None:
            job, selector = self.plan[self.previous['next']]
            if selector == 'latest' or selector.startswith('RUN'):
                name, args = 'shared_inspect_job_run', {
                    'selection': 'latest_completed' if selector == 'latest' else 'exact_run',
                    'selector': job if selector == 'latest' else selector,
                }
            else:
                name, args = 'shared_workspace_read', {'source_job': job, 'path': selector, 'offset_chars': 0}
            if name in self.tools[-1]:
                self.source = await self.call(tool_executor, name, args)
                return 'source read'
            self.source = dict(status='unavailable', reason='shared_read_capability_absent')
        self.saved = self.account()
        content = json.dumps(self.saved, ensure_ascii=False, separators=(',', ':'))
        self.saved_content = content
        if len(content) > 4000:
            raise AssertionError('fixture exceeded its own one-read state bound')
        result = await self.call(tool_executor, 'workspace_write', {
            'path': 'reflection.json', 'mode': 'upsert', 'content': content})
        self.write_confirmed = (result['status'] == 'applied' and result['published']
                                and result['durability_confirmed'])
        return 'write inspected'

    def account(self):
        state = deepcopy(self.previous)
        job, _ = self.plan[state['next']]
        source = self.source
        reference = None
        usable = source['status'] == 'ok' and source.get('job', {}).get('id') == int(job[3:])
        if usable and 'job_run' in source:
            run = source['job_run']
            reference = dict(job=job, run=f"RUN{run['id']}", status=run['status'],
                             finished_at=run['finished_at'], retrieved_at=source['retrieved_at'])
            usable = run['status'] == 'completed' and bool(source['report'])
        elif usable:
            artifact = source['artifact']
            reference = dict(job=job, path=artifact['path'], version=artifact['content_version'],
                             modified_at=artifact['modified_at'], retrieved_at=source['retrieved_at'])
            usable = not artifact['truncated']
        old = state['reviewed'].get(job)
        repeated = usable and old is not None and token(old) == token(reference)
        basis = [reference] if usable else []
        kind = 'limited'
        note = 'Source missing, failed, unavailable or partial; previous understanding retained.'
        if usable and not repeated:
            authored = EXAMPLES[self.example]
            basis = [ref for question in state['questions'] for ref in question['basis']] + [reference]
            state['account'] = authored['account']
            state['questions'] = ([dict(id='Q1', text=authored['question'], basis=basis)]
                                  if 'question' in authored else [])
            if 'retirement' in authored:
                state['retired'] = (state['retired'] + [dict(id='Q1', reason=authored['retirement'], basis=basis)])[-3:]
            state['reviewed'][job] = reference
            kind, note = authored['kind'], authored['note']
        elif repeated:
            kind, note = EXAMPLES['quiet']['kind'], EXAMPLES['quiet']['note']
        state.update(
            at=self.now.isoformat(),
            reflection=dict(kind=kind, note=note, basis=basis, current_observations=[]),
            next=(state['next'] + 1) % len(self.plan),
            attempt=dict(job=job, status=source['status'], reference=reference,
                         incorporated=usable, limitation=None if usable else note),
            omitted=['Not read this occurrence: ' + ', '.join(item[0] for item in self.plan if item[0] != job),
                     'Intermediate Runs/older versions not enumerated; no complete-through time.',
                     'No independently acquired current observations.'],
            suggested_followup=None,
        )
        return state


class ReflectiveContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 10, 17, 12, tzinfo=UTC)
        root = Path(self.temp.name)
        self.jobs = SQLiteJobStore(root / 'jobs.sqlite3', clock=lambda: self.now)
        self.workspace = FilesystemJobWorkspaceStore(root / 'workspaces')
        self.app = RobotApplication(
            RobotProfile('test', 'Test'), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True, shared_reads_enabled=True),
            platform_provider=Platform(), cognition_backend=ScriptedReflection([], 'first', self.now),
            job_store=self.jobs, job_workspace_store=self.workspace, wall_clock=lambda: self.now)
        await self.app.start()
        self.assertEqual(self.jobs.list_jobs(), ())  # No startup experiment seeding.
        self.sources = [self.jobs.create_job(name, enabled=False) for name in ('Camera review', 'Timestamp review')]
        self.plan = [(f'JOB{job.id}', 'latest') for job in self.sources]
        self.console = RuntimeConsole(self.app)
        description = DEFINITION['description'].format(sources=';'.join(':'.join(item) for item in self.plan))
        response, _ = self.console.execute('job add ' + shlex.quote(DEFINITION['name']) + ' --description ' + shlex.quote(description))
        self.reader = self.jobs.list_jobs()[-1]
        self.assertIn(f'Added JOB{self.reader.id}', response)
        self.console.execute(f'job disable JOB{self.reader.id}')
        self.console.execute(f'job enable JOB{self.reader.id}')

    async def asyncTearDown(self):
        await self.app.stop()
        self.jobs.close()
        self.workspace.close()
        self.temp.cleanup()

    def source_run(self, source, report, status=JobRunStatus.COMPLETED):
        run = self.jobs.create_run(source.id)
        self.jobs.transition_run(run.id, JobRunStatus.RUNNING)
        return self.jobs.transition_run(run.id, status, result_report=report)

    async def reflect(self, example='first', *, plan=None):
        self.now += timedelta(hours=1)
        if plan is not None:
            description = DEFINITION['description'].format(sources=';'.join(':'.join(item) for item in plan))
            response, _ = self.console.execute(f'job update JOB{self.reader.id} --description ' + shlex.quote(description))
            self.assertNotIn('Unable', response)
        provider = ScriptedReflection(plan or self.plan, example, self.now)
        self.app._cognition_backend = provider
        binding = self.app.start_job_run(self.reader.id)
        await self.app.work_job_run_once(binding.run.id)
        run = self.jobs.get_run(binding.run.id)
        self.assertLessEqual(sum(name in {'workspace_read', 'shared_workspace_read', 'shared_inspect_job_run'}
                                 for name, _, _ in provider.calls), 2)
        self.assertLessEqual(sum(name == 'workspace_write' for name, _, _ in provider.calls), 1)
        self.assertTrue(all({'orient_body', 'address_operator', 'deliver_report', 'play_earcon',
                             'schedule_followup', 'search_conversation_history', 'remember'}.isdisjoint(names)
                            for names in provider.tools))
        self.assertFalse(self.reader.procedure_enabled)
        self.assertIsNone(self.jobs.get_schedule(self.reader.id))
        return provider, run

    def state(self):
        return json.loads(self.workspace.read(self.reader.id, 'reflection.json').content)

    async def test_first_connection_revision_and_quiet_across_independent_runs(self):
        a1 = self.source_run(self.sources[0], EXAMPLES['source_reports']['A1'])
        first, run1 = await self.reflect()
        self.assertEqual(first.calls[0][2]['reason'], 'artifact_not_found')
        self.assertEqual(run1.status, JobRunStatus.COMPLETED)
        initial = self.state()
        self.assertEqual(initial['questions'][0]['id'], 'Q1')
        self.assertEqual(initial['reviewed'][self.plan[0][0]]['run'], f'RUN{a1.id}')
        self.assertNotIn(self.plan[1][0], initial['reviewed'])
        self.assertEqual(first.calls[2][2]['job']['id'], self.reader.id)
        b1 = self.source_run(self.sources[1], EXAMPLES['source_reports']['B1'])
        await self.reflect('connection')
        connected = self.state()
        self.assertNotEqual(initial['questions'][0]['text'], connected['questions'][0]['text'])
        self.assertEqual(len(connected['questions'][0]['basis']), 2)
        a2 = self.source_run(self.sources[0], EXAMPLES['source_reports']['A2'])
        await self.reflect('revision')
        revised = self.state()
        self.assertEqual(revised['questions'], [])
        retired_refs = {ref['run'] for ref in revised['retired'][0]['basis']}
        self.assertTrue({f'RUN{a1.id}', f'RUN{a2.id}', f'RUN{b1.id}'} <= retired_refs)
        self.assertEqual(revised['reviewed'][self.plan[0][0]]['run'], f'RUN{a2.id}')
        quiet, run4 = await self.reflect('quiet')
        final = self.state()
        self.assertEqual(final['reflection']['kind'], 'quiet')
        self.assertEqual(final['account'], revised['account'])
        self.assertEqual(final['reviewed'], revised['reviewed'])
        self.assertIsNone(final['suggested_followup'])
        self.assertEqual(len(quiet.tools), 4)  # state, source, one write, outcome; no continuation.
        self.assertEqual(json.loads(run4.result_report), final)
        self.assertEqual(len(self.jobs.list_runs(self.sources[0].id)), 2)
        self.assertEqual(len(self.jobs.list_runs(self.sources[1].id)), 1)
        self.assertEqual(self.workspace.list_entries(self.sources[0].id).entries, ())
        self.assertEqual(self.jobs.list_findings(), ())

    async def test_workspace_versions_are_exact_and_truncation_is_not_incorporated(self):
        source = self.sources[0]
        plan = [(f'JOB{source.id}', 'notes.md'), self.plan[1]]
        written = self.workspace.write(source.id, 'notes.md', 'create', EXAMPLES['source_reports']['A1'])
        await self.reflect(plan=plan)
        state = self.state()
        ref = state['reviewed'][plan[0][0]]
        self.assertEqual(ref['version'], written.content_version)
        self.assertNotIn('run', ref)
        self.workspace.write(source.id, 'notes.md', 'replace', 'x' * 8001)
        # The following occurrence chooses the other source; its absence is explicit.
        await self.reflect(plan=plan)
        await self.reflect(plan=plan)
        limited = self.state()
        self.assertEqual(limited['reflection']['kind'], 'limited')
        self.assertEqual(limited['account'], state['account'])
        self.assertEqual(limited['reviewed'][plan[0][0]], ref)
        self.assertFalse(limited['attempt']['incorporated'])

    async def test_missing_failed_no_report_and_unavailable_sources_preserve_coverage(self):
        self.source_run(self.sources[0], EXAMPLES['source_reports']['A1'])
        await self.reflect()
        previous = self.state()
        for situation in ('missing', 'failed', 'no_report', 'unavailable'):
            with self.subTest(situation=situation):
                baseline = deepcopy(previous)
                baseline['next'] = 0
                self.workspace.write(self.reader.id, 'reflection.json', 'upsert', json.dumps(baseline))
                plan = list(self.plan)
                if situation == 'missing':
                    plan[0] = ('JOB999', 'latest')
                elif situation == 'failed':
                    failed = self.source_run(self.sources[0], None, JobRunStatus.FAILED)
                    plan[0] = (plan[0][0], f'RUN{failed.id}')
                elif situation == 'no_report':
                    self.source_run(self.sources[0], None)
                else:
                    self.app.options = replace(self.app.options, shared_reads_enabled=False)
                _, run = await self.reflect(plan=plan)
                saved = self.state()
                self.assertEqual(run.status, JobRunStatus.COMPLETED)  # An honest limited reflection was saved.
                self.assertEqual(saved['reflection']['kind'], 'limited')
                self.assertEqual(saved['reviewed'], baseline['reviewed'])
                self.assertEqual(saved['account'], baseline['account'])
                self.assertEqual(saved['next'], 1)  # Rotation is attempt order, not coverage.
                self.assertFalse(saved['attempt']['incorporated'])
                self.app.options = replace(self.app.options, shared_reads_enabled=True)

    async def test_unreadable_or_truncated_previous_state_is_not_reset(self):
        for content in ('not JSON', 'x' * 8001):
            self.workspace.write(self.reader.id, 'reflection.json', 'upsert', content)
            before = self.workspace.read(self.reader.id, 'reflection.json')
            provider, run = await self.reflect()
            self.assertEqual(run.status, JobRunStatus.FAILED)
            self.assertFalse(any(name == 'workspace_write' for name, _, _ in provider.calls))
            self.assertFalse(any(name.startswith('shared_') for name, _, _ in provider.calls))
            self.assertEqual(self.workspace.read(self.reader.id, 'reflection.json'), before)

    async def test_source_storage_failure_is_limited_and_previous_storage_failure_stops(self):
        source = self.sources[0]
        plan = [(f'JOB{source.id}', 'notes.md'), self.plan[1]]
        self.workspace.write(source.id, 'notes.md', 'create', EXAMPLES['source_reports']['A1'])
        await self.reflect(plan=plan)
        baseline = self.state()
        baseline['next'] = 0
        self.workspace.write(self.reader.id, 'reflection.json', 'replace', json.dumps(baseline))
        actual_read = self.workspace.read
        def unavailable_source(job_id, *args, **kwargs):
            if job_id == source.id:
                raise WorkspaceBackendError('source backend unavailable')
            return actual_read(job_id, *args, **kwargs)
        with patch.object(self.workspace, 'read', side_effect=unavailable_source):
            await self.reflect(plan=plan)
        limited = self.state()
        self.assertEqual(limited['reflection']['kind'], 'limited')
        self.assertEqual(limited['reviewed'], baseline['reviewed'])
        with patch.object(self.workspace, 'read', side_effect=WorkspaceBackendError('unavailable')):
            provider, run = await self.reflect(plan=plan)
        self.assertEqual(run.status, JobRunStatus.FAILED)
        self.assertFalse(any(name == 'workspace_write' for name, _, _ in provider.calls))
        self.assertEqual(self.state(), limited)

    async def test_unsaved_candidate_cannot_advance_persisted_coverage(self):
        self.source_run(self.sources[0], EXAMPLES['source_reports']['A1'])
        baseline = dict(account='', questions=[], retired=[], reviewed={}, next=0)
        self.workspace.write(self.reader.id, 'reflection.json', 'upsert', json.dumps(baseline))
        with patch.object(self.workspace, 'write', side_effect=WorkspaceBackendError('unavailable')):
            provider, run = await self.reflect()
        self.assertEqual(run.status, JobRunStatus.FAILED)
        self.assertFalse(provider.write_confirmed)
        self.assertEqual(self.state(), baseline)

    async def test_three_sources_rotate_and_existing_operator_stop_controls_work(self):
        third = self.jobs.create_job('Third source', enabled=False)
        plan = self.plan + [(f'JOB{third.id}', 'latest')]
        for source in (*self.sources, third):
            self.source_run(source, EXAMPLES['source_reports']['A1'])
        for expected_index in range(3):
            await self.reflect(plan=plan)
            self.assertEqual(self.state()['next'], (expected_index + 1) % 3)
        self.assertEqual(len(self.state()['reviewed']), 3)
        scheduled, _ = self.console.execute(f'job schedule JOB{self.reader.id} daily 23:15 --timezone America/Toronto')
        self.assertIn('Scheduled', scheduled)
        self.console.execute(f'job unschedule JOB{self.reader.id}')
        live = self.app.start_job_run(self.reader.id)
        self.console.execute(f'job disable JOB{self.reader.id}')
        self.assertEqual(self.jobs.get_run(live.run.id).status, JobRunStatus.RUNNING)
        await self.console.execute_async(f'job stop RUN{live.run.id} "Pilot ended"')
        self.assertEqual(self.jobs.get_run(live.run.id).status, JobRunStatus.STOPPED)
        self.assertIsNone(self.jobs.get_schedule(self.reader.id))


class ReflectionSetupTests(unittest.TestCase):
    def test_definition_and_configuration_fit_existing_contracts(self):
        _, args, effective = parse_launch_arguments(['--config', str(ROOT / 'config/experiments/reflection-experiment.toml')])
        self.assertTrue(args.shared_reads_enabled)
        self.assertFalse(effective.initiative_actions)
        self.assertFalse(effective.initiative_messages)
        self.assertFalse(effective.initiative_continuation)
        self.assertFalse(effective.jobs_auto_continue)
        self.assertEqual(effective.hardware, 'virtual')
        store = SQLiteJobStore(':memory:')
        self.addCleanup(store.close)
        for sources in ('JOB7:latest;JOB9:latest', 'JOB7:latest;JOB9:notes.md;JOB11:RUN23'):
            description = DEFINITION['description'].format(sources=sources)
            job = store.create_job(DEFINITION['name'], description)
            self.assertLessEqual(len(description), 2000)
            self.assertEqual(len(description.splitlines()), 1)
            self.assertFalse(job.procedure_enabled)
            self.assertFalse(job.procedure_experiment)
