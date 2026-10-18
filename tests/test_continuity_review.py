"""Regression cases for discoverable requests and exact delivered report identity."""
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.cognition import CognitionToolCall
from embodied_runtime.console import RuntimeConsole, run_console_session
from embodied_runtime.conversation_history import SQLiteConversationHistoryStore
from embodied_runtime.conversation_history.tools import execute_history_tool
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.interaction import (
    CONSOLE_DIALOGUE, ConsoleOperatorMessageChannel, InteractionChannel,
    OperatorDeliveryDestination, OperatorDeliveryRoute, OperatorDeliveryRouteCatalog,
)
from embodied_runtime.jobs import FilesystemJobWorkspaceStore, JobRunStatus, SQLiteJobStore
from embodied_runtime.profile import RobotProfile
from embodied_runtime.sms import TwilioSmsService
from tests.test_conversation_history import NOW
from tests.test_job_result_retrieval import ScriptedBackend
from tests.test_operator_attention import Platform
from tests.test_report_delivery import ReportBackend
from tests.test_sms import settings


class ContinuityReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.history = SQLiteConversationHistoryStore(self.root / 'history.sqlite3')
        self.jobs = SQLiteJobStore(self.root / 'jobs.sqlite3')
        self.workspaces = FilesystemJobWorkspaceStore(self.root / 'workspaces')
        self.job = self.jobs.create_job('Review enclosure')
        self.apps = []

    async def asyncTearDown(self):
        for app in self.apps:
            await app.stop()
        self.history.close()
        self.jobs.close()
        self.workspaces.close()
        self.temp.cleanup()

    async def app(self, backend=None, sink=None):
        routes = () if sink is None else (OperatorDeliveryRoute(
            OperatorDeliveryDestination('output', sink.channel, 'configured operator output'), sink),)
        app = RobotApplication(RobotProfile('test', 'Test'), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True), platform_provider=Platform(),
            cognition_backend=backend or ScriptedBackend(), wall_clock=lambda: NOW,
            conversation_history_store=self.history, job_store=self.jobs,
            job_workspace_store=self.workspaces,
            operator_delivery_routes=OperatorDeliveryRouteCatalog(routes))
        self.apps.append(app)
        await app.start()
        return app

    def tool(self, name, arguments):
        return json.loads(execute_history_tool(self.history, self.jobs,
            CognitionToolCall(name, json.dumps(arguments))).output)

    def completed_run(self, text):
        run = self.jobs.create_run(self.job.id)
        self.jobs.transition_run(run.id, JobRunStatus.RUNNING)
        return self.jobs.transition_run(run.id, JobRunStatus.COMPLETED, result_report=text)

    async def restart(self, app):
        await app.stop()
        self.apps.remove(app)
        self.history = SQLiteConversationHistoryStore(self.root / 'history.sqlite3')
        self.jobs = SQLiteJobStore(self.root / 'jobs.sqlite3')
        self.workspaces = FilesystemJobWorkspaceStore(self.root / 'workspaces')

    async def test_old_console_start_discovered_without_python_evidence_and_after_restart(self):
        backend = ScriptedBackend()
        app = await self.app(backend)
        report, _ = await RuntimeConsole(app).execute_async(f'job start JOB{self.job.id}')
        printed_ref = re.search(r'Request: (REQ[0-9a-f]{32})', report)[1]
        for _ in range(6):
            await app.request_cognition('unrelated weather', interaction=CONSOLE_DIALOGUE)
        self.assertNotIn(printed_ref, [r['id'] for r in self.history.inspect_request('recent')['requests']])
        backend.calls = [('discover_requests', {'work_selector': f'JOB{self.job.id}', 'before': None, 'limit': 5})]
        await app.request_cognition('What happened to the Job I started earlier?', interaction=CONSOLE_DIALOGUE)
        discovered = backend.results[-1]['requests'][0]
        self.assertEqual(discovered['id'], printed_ref)
        self.assertEqual(discovered['work'][0]['kind'], 'run_started')
        run_id = discovered['work'][0]['run_id']
        # Inspect the reference returned by the acquisition, not an in-process attribute.
        backend.calls = [('inspect_request', {'selector': discovered['id']})]
        await app.request_cognition('Inspect that request', interaction=CONSOLE_DIALOGUE)
        self.assertEqual(backend.results[-1]['work'][0]['run_id'], run_id)
        await self.restart(app)
        result = self.tool('discover_requests', {'work_selector': f'RUN{run_id}', 'before': None, 'limit': 5})
        self.assertEqual(result['requests'][0]['id'], printed_ref)
        inspected = self.tool('inspect_request', {'selector': printed_ref})
        self.assertEqual(inspected['work'][0]['status'], 'interrupted')

    async def test_discovery_pagination_bounds_duplicate_calls_and_no_fallback(self):
        app = await self.app()
        for _ in range(9):
            await app.request_cognition('unrelated', interaction=CONSOLE_DIALOGUE)
        arguments = {'work_selector': None, 'before': None, 'limit': 5}
        first = self.tool('discover_requests', arguments)
        self.assertEqual(len(first['requests']), 5)
        self.assertEqual(first['next_before'], first['requests'][-1]['id'])
        await app.request_cognition('insert after first page', interaction=CONSOLE_DIALOGUE)
        second = self.tool('discover_requests', {**arguments, 'before': first['next_before']})
        self.assertEqual(len(second['requests']), 4)
        self.assertIsNone(second['next_before'])
        self.assertFalse(set(r['id'] for r in first['requests']) & set(r['id'] for r in second['requests']))
        self.assertEqual(self.tool('discover_requests', {**arguments, 'work_selector': 'RUN999'})['requests'], [])
        backend = ScriptedBackend([('discover_requests', arguments), ('discover_requests', arguments)])
        other = await self.app(backend)
        with patch.object(self.history, 'discover_requests', wraps=self.history.discover_requests) as discover:
            await other.request_cognition('Find older requests', interaction=CONSOLE_DIALOGUE)
        self.assertEqual(discover.call_count, 1)
        self.assertEqual(backend.results[0], backend.results[1])
        self.assertNotIn('discover_requests', [t.name for t in other._initiative_tools_for_episode(job_work=True)])
        for key, value in [('limit', 6), ('limit', True), ('before', 'bad'), ('work_selector', 'JOB0'),
                           ('work_selector', 'RUN1 OR 1=1'), ('work_selector', 'RUN9999999999999999999'), ('work_selector', 1)]:
            with self.subTest(key=key, value=value):
                self.assertEqual(self.tool('discover_requests', {**arguments, key: value})['status'], 'rejected')
        expired = self.tool('discover_requests', {**arguments, 'before': 'REQ' + '0' * 32})
        self.assertEqual(expired['reason'], 'cursor_expired')

    async def deliver_console(self, retrieval, before_delivery=None):
        channel = ConsoleOperatorMessageChannel()
        backend = ReportBackend(retrieval, destination='output', before_delivery=before_delivery)
        app = await self.app(backend, channel)
        class Terminal:
            def __init__(self):
                self.lines = iter(['ask Send that exact retained report.', None])
                self.written = []
            async def read_line(self, prompt): return next(self.lines, None)
            def write(self, text): self.written.append(text)
        terminal = Terminal()
        await run_console_session(RuntimeConsole(app), terminal, channel)
        self.assertEqual(backend.results[1]['status'], 'applied')
        request_id = self.history.inspect_request('recent')['requests'][0]['id']
        return app, request_id, terminal.written

    async def test_delivered_run2_identity_survives_restart_without_work_link_or_body_copy(self):
        self.completed_run('first unrelated report')
        target = self.completed_run('exact second report')
        app, request_id, written = await self.deliver_console(
            {'source_kind': 'job_run_result', 'selector': f'RUN{target.id}'},
            lambda: self.completed_run('later unrelated report'))
        self.assertIn('exact second report', ''.join(written))
        await self.restart(app)
        retained = self.tool('inspect_request', {'selector': request_id})
        self.assertEqual(retained['work'], [])
        report = next(d for d in retained['delivery'] if d['kind'] == 'report')
        self.assertEqual(report['status'], 'terminal_written')
        self.assertEqual(report['report_source'], {
            'source_kind': 'job_run_result', 'job_id': self.job.id, 'run_id': target.id,
            'path': None, 'content_version': None})
        self.assertNotIn('exact second report', json.dumps(report))

    async def test_workspace_report_source_is_acquired_version_not_later_file_version(self):
        path = 'reports/enclosure.txt'
        original = self.workspaces.write(self.job.id, path, 'create', 'original report')
        app, request_id, written = await self.deliver_console(
            {'source_kind': 'workspace_artifact', 'selector': f'JOB{self.job.id}', 'path': path},
            lambda: self.workspaces.write(self.job.id, path, 'replace', 'changed report'))
        self.assertIn('original report', ''.join(written))
        await self.restart(app)
        report = next(d for d in self.tool('inspect_request', {'selector': request_id})['delivery']
                      if d['kind'] == 'report')
        self.assertEqual(report['report_source']['path'], path)
        self.assertEqual(report['report_source']['content_version'], original.content_version)
        self.assertNotEqual(self.workspaces.read(self.job.id, path).content_version, original.content_version)
        self.assertEqual(report['status'], 'terminal_written')

    async def test_uncertain_sms_report_keeps_source_before_transport_without_retry(self):
        target = self.completed_run('short result')
        gateway = Mock()
        gateway.send.side_effect = TimeoutError()
        service = TwilioSmsService(settings(), None, gateway=gateway)
        service._accepting = True
        backend = ReportBackend({'source_kind': 'job_run_result', 'selector': f'RUN{target.id}'}, destination='output')
        app = await self.app(backend, service)
        await app.request_cognition('Send that report', interaction=CONSOLE_DIALOGUE)
        request_id = self.history.inspect_request('recent')['requests'][0]['id']
        report = self.tool('inspect_request', {'selector': request_id})['delivery'][0]
        self.assertEqual(report['kind'], 'report')
        self.assertEqual(report['status'], 'submission_unconfirmed')
        self.assertEqual(report['report_source']['run_id'], target.id)
        self.assertEqual(gateway.send.call_count, 1)

    async def test_v2_migration_leaves_existing_delivery_source_unavailable(self):
        app = await self.app()
        await app.request_cognition('hello', interaction=CONSOLE_DIALOGUE)
        request_id = self.history.inspect_request('recent')['requests'][0]['id']
        self.history.begin_delivery('DELlegacy', request_id, NOW, InteractionChannel.CONSOLE, 'message')
        self.history.finish_delivery('DELlegacy', NOW, 'terminal_written', None, None)
        await app.stop()
        self.apps.remove(app)
        connection = sqlite3.connect(self.root / 'history.sqlite3')
        connection.execute('ALTER TABLE delivery_attempts DROP COLUMN report_source')
        connection.execute('PRAGMA user_version = 2')
        connection.commit()
        connection.close()
        with patch.object(SQLiteConversationHistoryStore, '_cleanup', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                SQLiteConversationHistoryStore(self.root / 'history.sqlite3')
        connection = sqlite3.connect(self.root / 'history.sqlite3')
        try:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertNotIn('report_source', [row[1] for row in connection.execute('PRAGMA table_info(delivery_attempts)')])
        finally:
            connection.close()
        self.history = SQLiteConversationHistoryStore(self.root / 'history.sqlite3')
        retained = self.history.inspect_request(request_id)
        self.assertEqual(retained['delivery'][0]['status'], 'terminal_written')
        self.assertIsNone(retained['delivery'][0]['report_source'])
        self.assertEqual(retained['request']['id'], request_id)
        self.assertEqual(self.history._connection.execute('PRAGMA user_version').fetchone()[0], 4)
        self.assertEqual(len(self.history.search('hello')['matches']), 1)
