"""Process-level SIGINT tests with an actual foreground controlling PTY."""

import fcntl
import json
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest


# Replace composition only: exercise the real process, console, and cleanup
# boundaries while ensuring that no services, stores, or hardware can start.
DRIVER = r'''
import asyncio
import json
from pathlib import Path
import signal
import sys
from types import SimpleNamespace
from embodied_runtime import cli
from embodied_runtime.app import LifecycleState
from embodied_runtime.profile import RobotProfile

mode, policy, record = sys.argv[1:]
class Application:
    state = LifecycleState.CREATED
    def __init__(self):
        self.stopped = asyncio.Event()
    async def start(self):
        self.state = LifecycleState.RUNNING
        print('STARTED', flush=True)
    def request_stop(self):
        self.stopped.set()
    async def stop(self):
        print('STOPPING', flush=True)
        await asyncio.sleep(.15)
        self.state = LifecycleState.STOPPED
        print('STOPPED', flush=True)
    async def run(self):
        try:
            await self.start()
            await self.stopped.wait()
        finally:
            await self.stop()
class Console:
    heading = 'Signal test'
    prompt = 'test> '
    operator_message_prefix = 'Test'
    def __init__(self, *args, **kwargs):
        pass
    async def execute_async(self, command):
        with Path(record).open('a') as target:
            target.write(json.dumps(command) + '\n')
        if command == 'wait':
            print('EXECUTING', flush=True)
            try:
                await asyncio.sleep(.35)
            except asyncio.CancelledError:
                print('COMMAND_CANCELLED', flush=True)
                raise
            print('COMMAND_COMPLETED', flush=True)
        return '', command in ('exit', 'quit')
cli.RuntimeConsole = Console
async def application(args, profile, root, evidence, observability, shutdown, terminal=None):
    app = Application()
    if mode == 'console':
        return await cli._run_console_application(app, terminal, None)
    return await cli._run_headless_application(app, shutdown)
cli._run_application = application
originals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGWINCH)}
async def main():
    enabled = policy == 'enabled' or (policy == 'auto' and mode != 'console')
    return await cli._run_process_application(
        SimpleNamespace(mode=mode, ctrl_c_shutdown_enabled=enabled, no_color=True),
        RobotProfile('test', 'Test'), None, None, None)
result = cli._run_with_asyncio_cleanup(main())
assert all(signal.getsignal(sig) == value for sig, value in originals.items())
print('HANDLERS_RESTORED', flush=True)
sys.exit(result)
'''


def _controlling_terminal():
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


@unittest.skipUnless(os.name == 'posix', 'requires Unix signals and PTYs')
class ProcessSignalTests(unittest.TestCase):
    def launch(self, mode='console', policy='auto'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        record = Path(temporary.name) / 'commands.jsonl'
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 20, 50, 0, 0))
        attributes = termios.tcgetattr(slave)
        process = subprocess.Popen(
            [sys.executable, '-c', DRIVER, mode, policy, str(record)],
            stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, 'TERM': 'xterm-256color', 'PROMPT_TOOLKIT_NO_CPR': '1'},
            preexec_fn=_controlling_terminal,
        )
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        self.addCleanup(cleanup)
        self.output = b''
        return process, master, slave, attributes, record

    def read_until(self, master, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail(f'Timed out; PTY output: {self.output!r}')
            ready, _, _ = select.select([master], [], [], .02)
            if ready:
                self.output += os.read(master, 65536)

    def finish(self, process, master, slave, attributes):
        self.read_until(master, lambda: b'HANDLERS_RESTORED' in self.output)
        self.assertEqual(process.wait(timeout=3), 0, self.output)
        self.assertEqual(termios.tcgetattr(slave), attributes)
        self.assertEqual(self.output.count(b'STOPPING'), 1)
        self.assertEqual(self.output.count(b'STOPPED'), 1)
        self.assertNotIn(b'Traceback', self.output)

    def test_console_auto_clears_draft_and_does_not_cancel_running_command(self):
        process, master, slave, attributes, record = self.launch()
        self.read_until(master, lambda: b'test>' in self.output)
        os.write(master, b'partial')
        self.read_until(master, lambda: b'partial' in self.output)
        os.write(master, b'\x03\x03\x03')
        time.sleep(.04)
        self.assertIsNone(process.poll())
        os.write(master, b'wait\r')
        self.read_until(master, lambda: b'EXECUTING' in self.output)
        # Raw editing has finished; these keystrokes generate kernel SIGINT.
        self.assertTrue(termios.tcgetattr(slave)[3] & termios.ICANON)
        for _ in range(3):
            os.write(master, b'\x03')
            time.sleep(.025)
        self.read_until(master, lambda: b'COMMAND_COMPLETED' in self.output)
        self.assertNotIn(b'COMMAND_CANCELLED', self.output)
        self.read_until(master, lambda: not (termios.tcgetattr(slave)[3] & termios.ICANON))
        os.write(master, b'quit\r')
        self.finish(process, master, slave, attributes)
        self.assertEqual([json.loads(line) for line in record.read_text().splitlines()], ['wait', 'quit'])

    def test_console_enabled_interrupts_request_shutdown_during_execution(self):
        process, master, slave, attributes, record = self.launch(policy='enabled')
        self.read_until(master, lambda: b'test>' in self.output)
        os.write(master, b'wait\r')
        self.read_until(master, lambda: b'EXECUTING' in self.output)
        os.write(master, b'\x03')
        self.read_until(master, lambda: b'STOPPING' in self.output)
        for _ in range(3):
            os.kill(process.pid, signal.SIGINT)
            time.sleep(.01)
        self.finish(process, master, slave, attributes)
        self.assertIn(b'COMMAND_CANCELLED', self.output)

    def test_console_enabled_key_interrupt_exits_without_submitting_draft(self):
        process, master, slave, attributes, record = self.launch(policy='enabled')
        self.read_until(master, lambda: b'test>' in self.output)
        os.write(master, b'unsubmitted')
        self.read_until(master, lambda: b'unsubmitted' in self.output)
        os.write(master, b'\x03\x03\x03')
        self.finish(process, master, slave, attributes)
        self.assertFalse(record.exists())

    def test_headless_auto_interrupt_shutdown_and_disabled_sigterm(self):
        for policy in ('auto', 'enabled', 'disabled'):
            with self.subTest(policy=policy):
                process, master, slave, attributes, _ = self.launch('run', policy)
                self.read_until(master, lambda: b'STARTED' in self.output)
                os.write(master, b'\x03')
                if policy == 'disabled':
                    time.sleep(.06)
                    self.assertIsNone(process.poll())
                    os.kill(process.pid, signal.SIGTERM)
                self.read_until(master, lambda: b'STOPPING' in self.output)
                os.kill(process.pid, signal.SIGINT)
                os.kill(process.pid, signal.SIGINT)
                self.finish(process, master, slave, attributes)

    def test_console_sigterm_and_eof_restore_terminal(self):
        for ending in ('sigterm', 'eof', 'exit'):
            with self.subTest(ending=ending):
                process, master, slave, attributes, _ = self.launch(policy='disabled')
                self.read_until(master, lambda: b'test>' in self.output)
                if ending == 'sigterm':
                    os.kill(process.pid, signal.SIGTERM)
                else:
                    os.write(master, b'\x04' if ending == 'eof' else b'exit\r')
                self.finish(process, master, slave, attributes)
