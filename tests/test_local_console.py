"""Offline orchestration: real Unix sockets, dispatcher, stores and subprocesses."""

import asyncio
from contextlib import asynccontextmanager
import io
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cognition import CognitionToolCall
from embodied_runtime.console import RuntimeConsole
from embodied_runtime.console_client import ConsoleClient, discover, handshake, run_attached_session
from embodied_runtime.console_protocol import (
    ConsoleConnectionError, MAX_COMMAND, MAX_FRAME, VERSION, close_writer,
    console_directory, encode_frame, read_frame, validate_peer, write_frame,
)
from embodied_runtime.console_server import LocalConsoleServer, RETAIN_RESULTS, RETAIN_SECONDS
from embodied_runtime.console_style import ConsoleStyle
from embodied_runtime.cli import _run_application, parse_launch_arguments, validate_launch_dependencies
from embodied_runtime.conversation_history import SQLiteConversationHistoryStore
from embodied_runtime.conversation_history.continuity import CURRENT_REQUEST
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.interaction import (
    CONSOLE_NOTIFICATION, ConsoleOperatorMessageChannel, InteractionChannel,
    OperatorMessage,
    resolve_notification_route,
)
from embodied_runtime.jobs import SQLiteJobStore
from embodied_runtime.profile import RobotProfile
from tests.test_job_execution import JobBackend, Platform

ROOT = Path(__file__).resolve().parents[1]


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


class GateBackend(JobBackend):
    def __init__(self):
        super().__init__("completed")
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.cancelled = False

    async def respond(self, message, **kwargs):
        if self.block:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return await super().respond(message, **kwargs)


class Terminal:
    interactive = True
    style = ConsoleStyle(False)

    def __init__(self):
        self.output = ""
        self.lines = asyncio.Queue()

    def write(self, text):
        self.output += text

    async def display(self, text):
        self.write(text)

    async def read_line(self, prompt):
        return await self.lines.get()


class NotificationCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_headless_sms_route_survives_console_attach_and_detach(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / 'sockets'
            config = root / 'runtime.toml'
            config.write_text('[conversation_media]\nenabled=false\n')
            _, args, _ = parse_launch_arguments([
                '--config', str(config), '--mode', 'run', '--sms',
                '--hardware', 'virtual', '--cognition', 'openai-responses',
                '--initiative', '--initiative-messages',
                '--local-console-directory', str(directory),
            ])
            validate_launch_dependencies(args)
            sms = SimpleNamespace(channel=InteractionChannel.REMOTE_TEXT,
                                  start=AsyncMock(), stop=AsyncMock(), deliver=AsyncMock())
            app = None

            def compose(*positional, **kwargs):
                nonlocal app
                app = RobotApplication(*positional, **kwargs, platform_provider=Platform())
                return app

            shutdown = asyncio.Event()
            with patch('embodied_runtime.cli.RobotApplication', side_effect=compose), patch(
                'embodied_runtime.cli.TwilioSmsSettings.from_environment', return_value=object(),
            ), patch('embodied_runtime.cli.TwilioSmsService', return_value=sms), patch(
                'embodied_runtime.cli.build_cognition_backend', return_value=GateBackend(),
            ):
                runtime = asyncio.create_task(_run_application(
                    args, RobotProfile('test', 'Test Robot'), root / 'runs',
                    shutdown_requested=shutdown))
                client = None
                try:
                    await until(lambda: runtime.done() or (
                        directory.exists() and bool(list(directory.glob('*.sock')))))
                    if runtime.done():
                        await runtime
                        self.fail('runtime exited before console attachment')
                    self.assertIs(app._operator_message_sink, sms)
                    app.set_goal('Report operational status to the operator')
                    console = app._operator_delivery_routes.resolve('console').sink
                    self.assertIs(app._operator_delivery_routes.resolve('sms').sink, sms)
                    notification = resolve_notification_route(sms.channel)

                    async def notify(text, expected='applied'):
                        self.assertIs(app._operator_message_sink, sms)
                        result = await app._execute_initiative_tool(
                            CognitionToolCall('address_operator', json.dumps({'message': text})),
                            notification_interaction=notification)
                        self.assertEqual(json.loads(result.output)['status'], expected, result.output)
                        message = sms.deliver.await_args.args[0]
                        self.assertEqual(message.text, text)
                        self.assertEqual(message.interaction, notification)
                        self.assertTrue(console._messages.empty())

                    self.assertFalse(console.attached)
                    await notify('before attachment')
                    reader, writer, hello = await handshake(
                        next(directory.glob('*.sock')), purpose='attach')
                    terminal = Terminal()
                    client = ConsoleClient(reader, writer, hello, terminal)
                    self.assertTrue(console.attached)
                    await notify('while attached')
                    accepted = await client.request({
                        'type': 'command', 'id': 'sms-route-console-read', 'command': 'status'})
                    self.assertEqual(accepted['type'], 'accepted')
                    await client.result('sms-route-console-read')
                    self.assertIn('Response sms-route-console-read', terminal.output)

                    # Even a failed SMS delivery must not reroute to an attached console.
                    sms.deliver.side_effect = RuntimeError('SMS unavailable')
                    await notify('SMS failure while attached', expected='rejected')
                    sms.deliver.side_effect = None
                    await client.close()
                    client = None
                    await until(lambda: not console.attached)
                    await notify('after detachment')
                    self.assertEqual(sms.deliver.await_count, 4)
                finally:
                    if client is not None:
                        await client.close()
                    shutdown.set()
                    await asyncio.wait_for(runtime, 5)
            sms.start.assert_awaited_once()
            sms.stop.assert_awaited_once()


class LocalConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "console"
        self.jobs = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")
        self.history = SQLiteConversationHistoryStore(Path(self.temp.name) / "history.sqlite3")
        self.backend = GateBackend()
        self.messages = ConsoleOperatorMessageChannel()
        self.app = RobotApplication(
            RobotProfile("test", "Test Robot"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True, initiative_goal_closure_enabled=True),
            body_backend=VirtualBodyBackend(), platform_provider=Platform(),
            cognition_backend=self.backend, job_store=self.jobs,
            conversation_history_store=self.history,
        )
        await self.app.start()
        self.server = LocalConsoleServer(RuntimeConsole(self.app), self.messages, directory=self.directory)
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.close()
        await self.app.stop()

    @asynccontextmanager
    async def attach(self):
        reader, writer, hello = await handshake(self.server.path, purpose="attach")
        try:
            yield reader, writer, hello
        finally:
            await close_writer(writer)
            await until(lambda: self.server._attached is None)

    async def exchange(self, reader, writer, message):
        await write_frame(writer, message)
        return await asyncio.wait_for(read_frame(reader), 3)

    async def command(self, reader, writer, text, request_id="test-request"):
        accepted = await self.exchange(reader, writer, {"type": "command", "id": request_id, "command": text})
        self.assertEqual(accepted["type"], "accepted", accepted)
        return await self.response(reader, writer, request_id)

    async def response(self, reader, writer, request_id):
        async with asyncio.timeout(4):
            while True:
                state = await self.exchange(reader, writer, {"type": "status", "id": request_id})
                if state["state"] != "accepted":
                    break
                await asyncio.sleep(0.01)
        text, offset = "", 0
        while True:
            page = await self.exchange(reader, writer, {"type": "response", "id": request_id, "offset": offset})
            text += page["text"]
            offset = page["next"]
            if offset == page["chars"]:
                return text, state

    async def test_real_dispatch_mutation_once_and_response_mapping(self):
        async with self.attach() as (reader, writer, hello):
            self.assertEqual(hello["name"], "Test Robot")
            self.assertEqual(hello["connection"], "connected")
            report, _ = await self.command(reader, writer, "status", "read")
            self.assertIn("test", report.lower())
            report, state = await self.command(reader, writer, 'job add "Only once" --description "long description"', "mutation")
            self.assertIn("Added JOB1", report)
            self.assertIn("Request: " + state["runtime_request_id"], report)
            duplicate = await self.exchange(reader, writer, {"type": "command", "id": "mutation", "command": 'job add "Again"'})
            self.assertEqual(duplicate["type"], "rejected")
        async with self.attach() as (reader, writer, hello):
            recovered, _ = await self.response(reader, writer, "mutation")
            self.assertEqual(report, recovered)
            self.assertEqual(len(self.jobs.list_jobs()), 1)
            self.assertEqual(hello["instance_id"], self.server.instance_id)

    async def test_disconnect_during_ask_keeps_authority_and_reconnect_busy(self):
        self.backend.block = True
        session = self.app.continuity.session_id
        async with self.attach() as (reader, writer, _):
            accepted = await self.exchange(reader, writer, {"type": "command", "id": "ongoing", "command": "ask What's  happening?"})
            self.assertEqual(accepted["type"], "accepted")
            await self.backend.started.wait()
        self.assertFalse(self.backend.cancelled)
        async with self.attach() as (reader, writer, hello):
            self.assertEqual(hello["outstanding"], "ongoing")
            rejected = await self.exchange(reader, writer, {"type": "command", "id": "conflict", "command": "memory clear"})
            self.assertEqual(rejected["type"], "rejected")
            self.assertIn("busy", rejected["reason"])
            self.backend.release.set()
            report, _ = await self.response(reader, writer, "ongoing")
            self.assertIn("bounded work response", report)
            self.assertEqual(self.backend.requests[0][0], "What's  happening?")
            self.assertEqual(len(self.backend.requests), 1)
            self.assertEqual(len(self.app.working_memory.snapshot()), 1)
            self.assertEqual(self.app.continuity.session_id, session)

    async def test_lost_acceptance_ack_can_discover_work_without_replay(self):
        self.backend.block = True
        reader, writer, _ = await handshake(self.server.path, purpose="attach")
        await write_frame(writer, {"type": "command", "id": "lost-ack", "command": "ask hello"})
        await self.backend.started.wait()
        await close_writer(writer)
        await until(lambda: self.server._attached is None)
        async with self.attach() as (reader, writer, hello):
            self.assertEqual(hello["outstanding"], "lost-ack")
            self.backend.release.set()
            await self.response(reader, writer, "lost-ack")
            self.assertEqual(len(self.backend.requests), 1)

    async def test_manual_job_work_uses_existing_bounded_episode(self):
        async with self.attach() as (reader, writer, _):
            await self.command(reader, writer, 'job add "Review"', "add")
            await self.command(reader, writer, "job start JOB1", "start")
            report, state = await self.command(reader, writer, "job work", "work")
            self.assertIn("completed", report)
            self.assertIsNotNone(state["runtime_request_id"])
            count = len(self.backend.requests)
            self.assertGreater(count, 0)
            await self.response(reader, writer, "work")
            self.assertEqual(len(self.backend.requests), count)

    async def test_disconnect_during_manual_job_does_not_cancel_its_run(self):
        async with self.attach() as (reader, writer, _):
            await self.command(reader, writer, 'job add "Review"', "add")
            await self.command(reader, writer, "job start JOB1", "start")
            self.backend.block = True
            reply = await self.exchange(reader, writer, {"type": "command", "id": "job-work", "command": "job work"})
            self.assertEqual(reply["type"], "accepted")
            await self.backend.started.wait()
        self.assertFalse(self.backend.cancelled)
        async with self.attach() as (reader, writer, hello):
            self.assertEqual(hello["outstanding"], "job-work")
            self.backend.release.set()
            report, _ = await self.response(reader, writer, "job-work")
            self.assertIn("completed", report)
            self.assertEqual(len(self.jobs.list_runs(1)), 1)

    async def test_client_quit_eof_and_cancellation_detach_accepted_work(self):
        for termination in ("quit", None, "crash"):
            self.backend.started.clear()
            self.backend.release.clear()
            self.backend.block = True
            terminal = Terminal()
            reader, writer, hello = await handshake(self.server.path, purpose="attach")
            client = ConsoleClient(reader, writer, hello, terminal)
            session = asyncio.create_task(run_attached_session(client))
            await terminal.lines.put("ask waiting")
            await self.backend.started.wait()
            if termination == "crash":
                session.cancel()
            else:
                await terminal.lines.put(termination)
            await asyncio.gather(session, return_exceptions=True)
            await client.close()
            await until(lambda: self.server._attached is None)
            self.assertFalse(self.backend.cancelled)
            self.assertEqual(self.app.state.value, "running")
            self.backend.release.set()
            await until(lambda: self.server._active_id is None)

    async def test_client_recovers_output_and_receipt_without_cognition(self):
        async with self.attach() as (reader, writer, _):
            _, state = await self.command(reader, writer, "ask hello", "recover")
        terminal = Terminal()
        reader, writer, hello = await handshake(self.server.path, purpose="attach")
        client = ConsoleClient(reader, writer, hello, terminal)
        try:
            await client.result("recover", recovered=True)
            await until(lambda: self.server._results["recover"].terminal_written)
            self.assertIn("Recovered response recover", terminal.output)
            self.assertEqual(len(self.backend.requests), 1)
            retained = self.history.inspect_request(state["runtime_request_id"])
            self.assertEqual(retained["delivery"][-1]["status"], "terminal_written")
        finally:
            await client.close()

    async def test_second_client_legacy_slot_and_protocol_rejection(self):
        async with self.attach():
            with self.assertRaisesRegex(ConsoleConnectionError, "already attached"):
                await handshake(self.server.path, purpose="attach")
            found = await discover(self.directory)
            self.assertEqual(found[0][1]["connection"], "attached")
        self.server.legacy = True
        with self.assertRaisesRegex(ConsoleConnectionError, "embedded"):
            await handshake(self.server.path, purpose="attach")
        self.server.legacy = False
        reader, writer = await asyncio.open_unix_connection(str(self.server.path))
        try:
            await write_frame(writer, {"type": "hello", "version": 999, "purpose": "attach"})
            self.assertIn("version 1", (await read_frame(reader))["reason"])
        finally:
            await close_writer(writer)

    async def test_malformed_oversized_and_slow_handshakes_leave_runtime_alive(self):
        for payload in (struct.pack("!I", MAX_FRAME + 1), b'\x00\x00\x00\x01{', b'\x00'):
            reader, writer = await asyncio.open_unix_connection(str(self.server.path))
            writer.write(payload)
            await writer.drain()
            reply = await asyncio.wait_for(read_frame(reader), 4)
            self.assertEqual(reply["type"], "error")
            await close_writer(writer)
        async with self.attach() as (reader, writer, _):
            reply = await self.exchange(reader, writer, {"type": "command", "id": "large", "command": "x" * (MAX_COMMAND + 1)})
            self.assertEqual(reply["type"], "rejected")
            self.assertNotIn("large", self.server._results)
            await self.command(reader, writer, "status")

    async def test_client_rejects_malformed_or_incompatible_server_handshake(self):
        path = self.directory / 'agent-malformed.sock'
        reply = {'type': 'hello', 'version': VERSION}
        async def malformed(reader, writer):
            try:
                await read_frame(reader)
                await write_frame(writer, reply)
            finally:
                await close_writer(writer)
        listener = await asyncio.start_unix_server(malformed, path=str(path))
        path.chmod(0o600)
        try:
            with self.assertRaisesRegex(ConsoleConnectionError, 'malformed console handshake'):
                await handshake(path)
            reply['version'] = 999
            with self.assertRaisesRegex(ConsoleConnectionError, 'incompatible console handshake'):
                await handshake(path)
        finally:
            listener.close()
            await listener.wait_closed()
            path.unlink(missing_ok=True)

    async def test_large_response_chunking_and_retention_unknown_expired(self):
        large = "😀 界\n" * 30_000
        with patch.object(self.server.console, "execute_async", return_value=(large, False)):
            async with self.attach() as (reader, writer, _):
                report, _ = await self.command(reader, writer, "status", "large-reply")
                self.assertEqual(report, large)
                for index in range(RETAIN_RESULTS + 1):
                    await self.command(reader, writer, "status", f"request-{index}")
                self.assertEqual(len(self.server._results), RETAIN_RESULTS)
                reply = await self.exchange(reader, writer, {"type": "status", "id": "large-reply"})
                self.assertEqual(reply["state"], "unknown_or_expired")
                self.server._clock = lambda: __import__('time').monotonic() + RETAIN_SECONDS + 1
                reply = await self.exchange(reader, writer, {"type": "status", "id": f"request-{RETAIN_RESULTS}"})
                self.assertEqual(reply["state"], "unknown_or_expired")

    async def test_notification_detached_queued_and_terminal_delivery_are_truthful(self):
        evidence = self.app.continuity.begin(InteractionChannel.CONSOLE)
        token = CURRENT_REQUEST.set(evidence)
        try:
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                await self.messages.deliver(OperatorMessage("detached", "initiative", CONSOLE_NOTIFICATION))
        finally:
            CURRENT_REQUEST.reset(token)
        self.assertEqual(self.history.inspect_request(evidence.id)["delivery"][-1]["status"], "not_submitted")
        async with self.attach() as (reader, writer, _):
            token = CURRENT_REQUEST.set(evidence)
            try:
                await self.messages.deliver(OperatorMessage("visible", "initiative", CONSOLE_NOTIFICATION))
            finally:
                CURRENT_REQUEST.reset(token)
            notification = await read_frame(reader)
            self.assertEqual(notification["type"], "notification")
            self.assertEqual(self.history.inspect_request(evidence.id)["delivery"][-1]["status"], "queued")
            await self.exchange(reader, writer, {"type": "receipt", "id": notification["id"], "status": "terminal_written"})
            self.assertEqual(self.history.inspect_request(evidence.id)["delivery"][-1]["status"], "terminal_written")
        self.assertFalse(self.messages.attached)

    async def test_notification_backpressure_and_slow_write_are_bounded(self):
        from embodied_runtime.console_server import _Connection
        class SlowWriter:
            def write(self, data): pass
            async def drain(self): await asyncio.Event().wait()
        with patch('embodied_runtime.console_server.IO_TIMEOUT', .03):
            with self.assertRaises(TimeoutError):
                await _Connection(SlowWriter()).send({"type": "test"})
        self.messages.set_attached(True)
        message = OperatorMessage("bounded", "initiative", CONSOLE_NOTIFICATION)
        for _ in range(16):
            await self.messages.deliver(message)
        with self.assertRaises(RuntimeError):
            await self.messages.deliver(message)
        self.messages.set_attached(False)
        self.assertTrue(self.messages._messages.empty())

    async def test_unacknowledged_notifications_disconnect_without_backlog(self):
        reader, writer, _ = await handshake(self.server.path, purpose="attach")
        try:
            for _ in range(17):
                await self.messages.deliver(OperatorMessage("bounded", "initiative", CONSOLE_NOTIFICATION))
                await asyncio.sleep(.01)
            await until(lambda: self.server._attached is None)
            self.assertFalse(self.messages.attached)
            self.assertTrue(self.messages._messages.empty())
            self.assertEqual(self.app.state.value, 'running')
        finally:
            await close_writer(writer)

    async def test_real_slow_socket_cannot_hold_runtime_or_attachment_open(self):
        reader, writer, _ = await handshake(self.server.path, purpose='attach')
        writer.transport.pause_reading()
        self.server._attached.writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        try:
            for _ in range(10):
                await self.messages.deliver(OperatorMessage('a' * 64_000, 'initiative', CONSOLE_NOTIFICATION))
                await asyncio.sleep(.01)
            await until(lambda: self.server._attached is None)
            async with self.attach() as (new_reader, new_writer, _):
                await self.command(new_reader, new_writer, 'status')
        finally:
            await close_writer(writer)

    async def test_client_receives_notification_while_draft_waits_and_reports_runtime_loss(self):
        terminal = Terminal()
        reader, writer, hello = await handshake(self.server.path, purpose='attach')
        client = ConsoleClient(reader, writer, hello, terminal)
        session = asyncio.create_task(run_attached_session(client))
        try:
            await self.messages.deliver(OperatorMessage('notification', 'initiative', CONSOLE_NOTIFICATION))
            await until(lambda: 'Test Robot: notification' in terminal.output)
            self.assertFalse(session.done())
            await self.server.close()
            with self.assertRaisesRegex(ConsoleConnectionError, 'Runtime connection lost'):
                await session
        finally:
            session.cancel()
            await asyncio.gather(session, return_exceptions=True)
            await client.close()

    async def test_shutdown_removes_only_owned_endpoint_and_new_instance_has_no_old_results(self):
        async with self.attach() as (reader, writer, _):
            await self.command(reader, writer, "status", "old")
        old_id, old_path = self.server.instance_id, self.server.path
        await self.server.close()
        self.assertFalse(old_path.exists())
        self.server = LocalConsoleServer(RuntimeConsole(self.app), self.messages, directory=self.directory)
        await self.server.start()
        self.assertNotEqual(self.server.instance_id, old_id)
        async with self.attach() as (reader, writer, hello):
            self.assertEqual(hello["recent"], [])
            reply = await self.exchange(reader, writer, {"type": "status", "id": "old"})
            self.assertEqual(reply["state"], "unknown_or_expired")
        # A replacement regular file or symlink is never removed by cleanup.
        self.server.path.unlink()
        self.server.path.write_text("do not delete")
        await self.server.close()
        self.assertEqual(self.server.path.read_text(), "do not delete")

    async def test_stale_unsafe_endpoint_and_same_uid_peer_validation(self):
        stale = self.directory / "agent-stale.sock"
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(stale))
        stale.chmod(0o600)
        sock.close()
        unsafe = self.directory / "agent-link.sock"
        unsafe.symlink_to(self.server.path)
        found = await discover(self.directory)
        self.assertEqual(sum(i is not None for _, i, _ in found), 1)
        self.assertTrue(stale.exists())
        self.assertTrue(unsafe.is_symlink())
        class Peer:
            def getsockopt(self, *args):
                return struct.pack("3i", 123, os.geteuid() + 1, 1)
        class Writer:
            def get_extra_info(self, key): return Peer()
        with self.assertRaisesRegex(ConsoleConnectionError, "different user"):
            validate_peer(Writer())
        with patch('embodied_runtime.console_protocol.validate_peer', side_effect=ConsoleConnectionError('peer mismatch')):
            with self.assertRaisesRegex(ConsoleConnectionError, 'peer mismatch'):
                await handshake(self.server.path)
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(ConsoleConnectionError, '0700'):
            console_directory(self.directory)
        self.directory.chmod(0o700)
        with patch('embodied_runtime.console_server.validate_peer', side_effect=ConsoleConnectionError('server peer mismatch')):
            with self.assertRaisesRegex(ConsoleConnectionError, 'server peer mismatch'):
                await handshake(self.server.path)

    async def test_endpoint_collision_is_never_unlinked(self):
        other = LocalConsoleServer(RuntimeConsole(self.app), ConsoleOperatorMessageChannel(), directory=self.directory)
        other.instance_id = self.server.instance_id
        with self.assertRaisesRegex(ConsoleConnectionError, 'setup failed'):
            await other.start()
        self.assertTrue(self.server.path.is_socket())
        async with self.attach() as (reader, writer, _):
            await self.command(reader, writer, 'status')


class ProcessConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'sockets'
        self.log = tempfile.TemporaryFile()
        self.addCleanup(self.log.close)
        config = Path(self.temp.name) / 'runtime.toml'
        config.write_text('[jobs]\nenabled=true\ndatabase_path="jobs.sqlite3"\n')
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / 'main.py'), '--config', str(config),
            '--mode', 'run', '--hardware', 'virtual', '--no-earcons',
            '--local-console-directory', str(self.directory),
            cwd=self.temp.name, stdout=self.log, stderr=self.log,
            env={**os.environ, 'XDG_RUNTIME_DIR': '/service-environment-only'},
        )
        await until(lambda: self.directory.exists() and bool(list(self.directory.glob('*.sock'))) or self.process.returncode is not None)
        if self.process.returncode is not None:
            self.log.seek(0)
            self.fail(self.log.read().decode())
        self.path = next(self.directory.glob('*.sock'))
        _, writer, self.identity = await handshake(self.path)
        await close_writer(writer)

    async def asyncTearDown(self):
        if self.process.returncode is None:
            self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), 8)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
            self.fail('virtual runtime did not exit after SIGTERM')

    async def run_client(self, commands, *options):
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / 'console.py'), '--directory', str(self.directory), *options,
            cwd=self.temp.name, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={k: v for k, v in os.environ.items() if k not in ('XDG_RUNTIME_DIR', 'OPENAI_API_KEY')},
        )
        output, error = await asyncio.wait_for(process.communicate(commands.encode()), 8)
        return process.returncode, output.decode(), error.decode()

    async def test_separate_root_entrypoint_plain_commands_mutation_detach_and_runtime_shutdown(self):
        code, output, error = await self.run_client('status\njob add "Keep running"\njob start JOB1\nquit\n')
        self.assertEqual((code, error), (0, ''))
        self.assertIn('Added JOB1', output)
        self.assertIn('Started JOB1', output)
        self.assertNotIn('\x1b', output)
        self.assertIn(self.identity['instance_id'], output)
        code, output, error = await self.run_client('job current\n')
        self.assertEqual(code, 0, error)
        self.assertIn('JOB1', output)
        self.assertIn(self.identity['instance_id'], output)
        reader, writer, _ = await handshake(self.path, purpose='attach')
        self.process.terminate()
        await asyncio.wait_for(self.process.wait(), 8)
        with self.assertRaises(ConsoleConnectionError):
            await asyncio.wait_for(read_frame(reader), 3)
        await close_writer(writer)
        self.assertFalse(self.path.exists())

    async def test_no_agents_multiple_agents_selection_and_absolute_client_imports(self):
        empty = Path(self.temp.name) / 'empty'
        console_directory(empty, create=True)
        code, _, error = await self.run_client('', '--directory', str(empty))
        self.assertNotEqual(code, 0)
        self.assertIn('Start main.py', error)
        self.assertNotIn('Traceback', error)
        # A real second runtime endpoint with a distinct instance identity.
        other = LocalConsoleServer(type('Console', (), {'prompt': 'other> ', 'operator_message_prefix': 'Other'})(), ConsoleOperatorMessageChannel(), directory=self.directory)
        await other.start()
        try:
            code, output, error = await self.run_client('quit\n')
            self.assertNotEqual(code, 0)
            self.assertIn('Multiple agents', error)
            self.assertIn(other.instance_id, output)
            code, output, error = await self.run_client('quit\n', '--agent', self.identity['instance_id'])
            self.assertEqual(code, 0, error)
        finally:
            await other.close()
        process = await asyncio.create_subprocess_exec(sys.executable, '-c',
            'import sys; sys.path.insert(0, sys.argv[1]); import embodied_runtime.console_client; '
            'assert not any(x in sys.modules for x in ("embodied_runtime.app", "embodied_runtime.config", "embodied_runtime.jobs", "embodied_runtime.cognition", "sqlite3", "PIL"))',
            str(ROOT), cwd=self.temp.name)
        self.assertEqual(await process.wait(), 0)

    async def test_detached_ctrl_c_and_client_crash_preserve_runtime_and_job(self):
        code, _, error = await self.run_client('job add "Intact"\njob start JOB1\nquit\n')
        self.assertEqual(code, 0, error)
        master, slave = os.openpty()
        os.set_blocking(master, False)
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / 'console.py'), '--directory', str(self.directory), '--no-color',
            cwd=self.temp.name, stdin=slave, stdout=slave, stderr=slave)
        output = bytearray()
        async def wait_text(text):
            async with asyncio.timeout(5):
                while text not in output:
                    try:
                        output.extend(os.read(master, 65536))
                    except BlockingIOError:
                        await asyncio.sleep(.01)
        try:
            await wait_text(b'mira>')
            os.write(master, b'job add "Must not execute"')
            await wait_text(b'Must not execute')
            process.send_signal(signal.SIGINT)
            await asyncio.sleep(.05)
            os.write(master, b'status\r')
            await wait_text(b'Response ')
            process.kill()
            await process.wait()
            await asyncio.sleep(.05)
            code, report, error = await self.run_client('jobs\njob current\nquit\n')
            self.assertEqual(code, 0, error)
            self.assertIn(self.identity['instance_id'], report)
            self.assertIn('JOB1', report)
            self.assertNotIn('JOB2', report)
            self.assertIn('RUN1', report)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            os.close(master)
            os.close(slave)

    async def test_real_combined_console_is_exclusive_and_owns_shutdown(self):
        paths = set(self.directory.glob('*.sock'))
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / 'main.py'), '--console', '--hardware', 'virtual', '--no-earcons',
            '--local-console-directory', str(self.directory),
            cwd=self.temp.name, stdin=asyncio.subprocess.PIPE, stdout=self.log, stderr=self.log)
        try:
            await until(lambda: bool(set(self.directory.glob('*.sock')) - paths))
            path = next(iter(set(self.directory.glob('*.sock')) - paths))
            _, writer, identity = await handshake(path)
            await close_writer(writer)
            self.assertEqual(identity['connection'], 'embedded')
            with self.assertRaisesRegex(ConsoleConnectionError, 'embedded'):
                await handshake(path, purpose='attach')
            process.stdin.write(b'quit\n')
            await process.stdin.drain()
            self.assertEqual(await asyncio.wait_for(process.wait(), 5), 0)
            self.assertFalse(path.exists())
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def test_endpoint_setup_failure_is_fatal_and_actionable(self):
        unsafe = Path(self.temp.name) / 'unsafe'
        unsafe.mkdir(mode=0o755)
        unsafe.chmod(0o755)
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / 'main.py'), '--mode', 'run', '--hardware', 'virtual',
            '--local-console-directory', str(unsafe),
            cwd=self.temp.name, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, error = await asyncio.wait_for(process.communicate(), 5)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'--no-local-console', error)
        self.assertNotIn(b'Traceback', error)
        self.assertFalse(list(unsafe.glob('*.sock')))


class ConsolePathTests(unittest.TestCase):
    def test_configuration_precedence_and_strict_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'runtime.toml'
            config.write_text(f'[runtime]\nlocal_console_enabled=false\nlocal_console_directory="{temp}"\n')
            _, args, effective = parse_launch_arguments(['--config', str(config)])
            self.assertFalse(args.local_console_enabled)
            self.assertEqual(args.local_console_directory, Path(temp))
            _, args, _ = parse_launch_arguments(['--config', str(config), '--local-console', '--local-console-directory', '/tmp/override'])
            self.assertTrue(args.local_console_enabled)
            self.assertEqual(args.local_console_directory, Path('/tmp/override'))
            for value in ('local_console_enabled="yes"', 'local_console_directory="relative"', 'local_console_typo=true'):
                config.write_text('[runtime]\n' + value)
                with patch('sys.stderr'), self.assertRaises(SystemExit):
                    parse_launch_arguments(['--config', str(config)])

    def test_directory_policy_ignores_environment_and_rejects_symlinks(self):
        from types import SimpleNamespace
        expected = Path(f'/tmp/embodied-runtime-{os.geteuid()}')
        original = Path.lstat
        def lstat(path):
            return SimpleNamespace(st_uid=os.geteuid(), st_mode=stat.S_IFDIR | 0o700) if path == expected else original(path)
        # Resolve without creating a real default endpoint in this offline test.
        with patch.object(Path, 'lstat', lstat):
            for environment in ({'HOME': '/shell', 'TMPDIR': '/shell/tmp', 'XDG_RUNTIME_DIR': '/shell/run'}, {}):
                with patch.dict(os.environ, environment, clear=True):
                    self.assertEqual(console_directory(), expected)
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'target'
            console_directory(target, create=True)
            link = Path(temp) / 'link'
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(ConsoleConnectionError):
                console_directory(link)
            with self.assertRaises(ConsoleConnectionError):
                console_directory(link / 'nested', create=True)
            with self.assertRaises(ConsoleConnectionError):
                console_directory(Path('relative'))


if __name__ == '__main__':
    unittest.main()
