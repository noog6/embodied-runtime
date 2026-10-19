"""Real PTY keystrokes and screen rendering without robot hardware."""

import asyncio
import fcntl
import io
import logging
import os
import signal
import struct
import sys
import termios
import unittest
from unittest.mock import patch

try:
    import pyte
except ImportError:
    pyte = None

from embodied_runtime.console import run_console_session
from embodied_runtime.terminal import AsyncLineTerminal, SessionHistory
from embodied_runtime.interaction import (
    CONSOLE_NOTIFICATION, ConsoleOperatorMessageChannel, OperatorMessage,
)


class HistoryTests(unittest.TestCase):
    def test_bounded_exact_nonblank_history(self):
        history = SessionHistory()
        for index in range(510):
            history.append_string(f' job add "{index}"  ')
        history.append_string(' \t ')
        history.append_string(' job add "509"  ')
        self.assertEqual(len(history.get_strings()), 500)
        self.assertEqual(history.get_strings()[0], ' job add "11"  ')
        self.assertEqual(history.get_strings()[-2:], [' job add "509"  '] * 2)
        self.assertEqual(SessionHistory().get_strings(), [])


@unittest.skipUnless(os.name == 'posix', 'requires Unix PTYs')
class EditorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.master, self.slave = os.openpty()
        os.set_blocking(self.master, False)
        self.resize(32, 14, notify=False)
        self.attributes = termios.tcgetattr(self.slave)
        self.reader = os.fdopen(os.dup(self.slave), 'r', encoding='utf-8')
        self.output = os.fdopen(os.dup(self.slave), 'w', encoding='utf-8')
        self.environment = patch.dict(os.environ, {
            'TERM': 'xterm-256color', 'PROMPT_TOOLKIT_NO_CPR': '1',
        })
        self.environment.start()
        self.terminal = AsyncLineTerminal(self.reader, self.output, no_color=True)
        self.tasks = []
        self.screen = pyte.Screen(32, 14) if pyte else None
        self.stream = pyte.ByteStream(self.screen) if pyte else None
        self.rendered = b''

    def resize(self, columns, rows, *, notify=True):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
        if notify:
            if self.screen:
                self.screen.resize(rows, columns)
            os.kill(os.getpid(), signal.SIGWINCH)

    def drain(self):
        while True:
            try:
                data = os.read(self.master, 65536)
            except BlockingIOError:
                break
            if not data:
                break
            self.rendered += data
            if self.stream:
                self.stream.feed(data)

    async def until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                self.drain()
                await asyncio.sleep(.005)
        self.drain()

    def send(self, text):
        os.write(self.master, text.encode() if isinstance(text, str) else text)

    @property
    def buffer(self):
        return self.terminal._session.default_buffer

    async def text(self, expected, cursor=None):
        try:
            await self.until(lambda: self.buffer.text == expected and (
                cursor is None or self.buffer.cursor_position == cursor))
        except TimeoutError:
            self.fail(f'Expected {len(expected)} chars at {cursor}; got {len(self.buffer.text)} chars '
                      f'at {self.buffer.cursor_position}: {self.buffer.text[:80]!r}')

    async def prompt(self):
        task = asyncio.create_task(self.terminal.read_line('robot> '))
        self.tasks.append(task)
        await self.until(lambda: self.terminal._session.app.is_running and not (
            termios.tcgetattr(self.slave)[3] & termios.ICANON))
        return task

    async def submit(self, task, expected):
        self.send('\r')
        self.assertEqual(await asyncio.wait_for(task, 3), expected)
        self.drain()

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.assertEqual(termios.tcgetattr(self.slave), self.attributes)
        self.reader.close()
        self.output.close()
        os.close(self.slave)
        os.close(self.master)
        self.environment.stop()

    async def test_history_draft_boundaries_and_editing_copy(self):
        with self.terminal.activate():
            for command in (' first "quoted"  ', 'second'):
                task = await self.prompt()
                self.send(command)
                await self.text(command)
                await self.submit(task, command)
            task = await self.prompt()
            self.send('unfinished draft\x1b[A')
            await self.text('second')
            self.send('\x1b[A\x1b[A')
            await self.text(' first "quoted"  ')
            self.send('\x1b[Fedited')
            await self.text(' first "quoted"  edited')
            self.send('\x1b[B\x1b[B\x1b[B')
            await self.text('unfinished draft')
            self.send('\x1b[A\x1b[A')
            await self.text(' first "quoted"  edited')
            await self.submit(task, ' first "quoted"  edited')
            self.assertEqual(self.terminal._session.history.get_strings(), [
                ' first "quoted"  ', 'second', ' first "quoted"  edited'])
            task = await self.prompt()
            self.send('\x1b[A\x1b[A\x1b[A')
            await self.text(' first "quoted"  ')
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_cursor_unicode_insertion_backspace_delete_home_end(self):
        with self.terminal.activate():
            task = await self.prompt()
            # Deliver a multibyte character in separate OS reads.
            self.send(b'a\xc3')
            await asyncio.sleep(.02)
            self.send(b'\xa9\xe7\x95\x8cZ')
            await self.text('aé界Z', 4)
            self.send('\x1b[D\x1b[D!\x7f\x1b[3~')
            await self.text('aéZ', 2)
            self.send('\x1b[C?\x1b[H>\x1b[F<')
            await self.text('>aéZ?<', 6)
            await self.submit(task, '>aéZ?<')

    async def test_long_wrapped_input_resize_and_async_output(self):
        with self.terminal.activate():
            task = await self.prompt()
            command = 'job add "' + 'café 界 ' * 15 + '" --description "a long job"'
            self.send(command)
            await self.text(command)
            self.send('\x1b[H' + '\x1b[C' * 12)
            await self.text(command, 12)
            await self.terminal.display('\noperator: incoming\n')
            self.assertEqual(self.buffer.text, command)
            self.assertEqual(self.buffer.cursor_position, 12)
            self.resize(23, 18)
            await asyncio.sleep(.08)
            self.drain()
            if self.screen:
                self.assertIn('operator: incoming', '\n'.join(self.screen.display))
                cursor = self.screen.cursor
                # Seven prompt cells plus twelve command cells (é is one cell).
                self.assertEqual((cursor.x, cursor.y), (19, 2))
            self.send('X')
            await self.text(command[:12] + 'X' + command[12:])
            await self.submit(task, command[:12] + 'X' + command[12:])
            task = await self.prompt()
            self.send('\x1b[A\x1b[Hrecalled ')
            await self.text('recalled ' + command[:12] + 'X' + command[12:])
            await self.submit(task, 'recalled ' + command[:12] + 'X' + command[12:])

    async def test_input_taller_than_screen_remains_one_command(self):
        with self.terminal.activate():
            task = await self.prompt()
            command = 'aé界 ' * 120
            self.send('\x1b[200~' + command + '\x1b[201~')
            await self.text(command)
            self.send('\x1b[Hstart \x1b[Fend')
            await self.text('start ' + command + 'end')
            await self.terminal.display('\noperator: another message\n')
            self.send('\x1b[H>')
            await self.text('>start ' + command + 'end')
            await self.submit(task, '>start ' + command + 'end')

    async def test_repeated_ctrl_c_clears_draft_and_history_navigation(self):
        with self.terminal.activate():
            task = await self.prompt()
            self.send('original')
            await self.text('original')
            await self.submit(task, 'original')
            task = await self.prompt()
            self.send('draft\x1b[A')
            await self.text('original')
            self.send('\x03\x03\x03')
            await self.text('', 0)
            self.send('\x1b[B')
            await self.text('', 0)
            self.assertFalse(task.done())
            self.send('fresh')
            await self.text('fresh')
            await self.submit(task, 'fresh')
            self.assertEqual(self.terminal._session.history.get_strings(), ['original', 'fresh'])

    async def test_eof_and_exception_restore_terminal(self):
        with self.terminal.activate():
            task = await self.prompt()
            self.send('\x04')
            self.assertIsNone(await asyncio.wait_for(task, 3))
        self.assertEqual(termios.tcgetattr(self.slave), self.attributes)
        with self.assertRaisesRegex(RuntimeError, 'failure'):
            with self.terminal.activate():
                task = await self.prompt()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                raise RuntimeError('failure')

    async def test_python_resize_handler_and_startup_failure_are_restored(self):
        old = signal.getsignal(signal.SIGWINCH)
        def resize_handler(signum, frame):
            pass
        signal.signal(signal.SIGWINCH, resize_handler)
        try:
            with self.terminal.activate():
                task = await self.prompt()
                self.send('\x04')
                self.assertIsNone(await task)
            self.assertIs(signal.getsignal(signal.SIGWINCH), resize_handler)
            with patch('embodied_runtime.terminal.PromptSession', side_effect=RuntimeError('startup')):
                with self.assertRaisesRegex(RuntimeError, 'startup'):
                    with self.terminal.activate():
                        self.fail('startup should fail')
            self.assertIs(signal.getsignal(signal.SIGWINCH), resize_handler)
        finally:
            signal.signal(signal.SIGWINCH, old)

    async def test_bracketed_paste_remains_one_logical_line(self):
        with self.terminal.activate():
            task = await self.prompt()
            self.send('\x1b[200~one\r\ntwo\nthree\x1b[201~')
            await self.text('one two three')
            self.assertFalse(task.done())
            await self.submit(task, 'one two three')

    async def test_duplicated_log_stream_preserves_draft(self):
        duplicate = os.fdopen(os.dup(self.slave), 'w')
        handler = logging.StreamHandler(duplicate)
        root = logging.getLogger()
        root.addHandler(handler)
        original_stderr = sys.stderr
        try:
            # Simulate configure_logging's stable duplicated stderr stream.
            with self.terminal.activate():
                self.assertIs(sys.stderr, original_stderr)
                task = await self.prompt()
                self.send('unfinished\x1b[D\x1b[D')
                await self.text('unfinished', 8)
                logging.getLogger('terminal-test').warning('runtime log arrives')
                await self.until(lambda: b'runtime log arrives' in self.rendered)
                self.assertEqual(self.buffer.cursor_position, 8)
                self.send('!')
                await self.text('unfinish!ed')
                await self.submit(task, 'unfinish!ed')
            self.assertIs(handler.stream, duplicate)
            self.assertIs(sys.stderr, original_stderr)
        finally:
            root.removeHandler(handler)
            handler.close()
            duplicate.close()

    async def test_output_flush_failure_still_restores_routing(self):
        class BrokenFlushHandler(logging.StreamHandler):
            fail = False
            def flush(self):
                if self.fail:
                    raise OSError('output flush failed')
                super().flush()
        duplicate = os.fdopen(os.dup(self.slave), 'w')
        handler = BrokenFlushHandler(duplicate)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with patch('sys.stdout', self.output):
                with self.assertRaisesRegex(OSError, 'output flush failed'):
                    with self.terminal.activate():
                        task = await self.prompt()
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        handler.fail = True
                self.assertIs(sys.stdout, self.output)
                self.assertIs(handler.stream, duplicate)
        finally:
            handler.fail = False
            root.removeHandler(handler)
            handler.close()
            duplicate.close()

    async def test_messages_keep_same_input_task_and_simultaneous_eof(self):
        channel = ConsoleOperatorMessageChannel()
        executed = []
        class Console:
            heading = 'Test console'
            prompt = 'robot> '
            operator_message_prefix = 'Robot'
            async def execute_async(self, line):
                executed.append(line)
                return '', line == 'exit'
        session = asyncio.create_task(run_console_session(Console(), self.terminal, channel))
        self.tasks.append(session)
        await self.until(lambda: self.terminal._session is not None and self.terminal._session.app.is_running)
        self.send('partially typed\x1b[D')
        await self.text('partially typed', 14)
        input_tasks = {t for t in asyncio.all_tasks() if t.get_coro().__qualname__ == 'AsyncLineTerminal.read_line'}
        await channel.deliver(OperatorMessage('message one', 'initiative', CONSOLE_NOTIFICATION))
        await self.until(lambda: b'message one' in self.rendered)
        self.assertEqual(input_tasks, {t for t in asyncio.all_tasks() if t.get_coro().__qualname__ == 'AsyncLineTerminal.read_line'})
        self.send('!\r')
        await self.until(lambda: executed == ['partially type!d'])
        await self.until(lambda: self.terminal._session.app.is_running)
        self.send('\x04')
        await channel.deliver(OperatorMessage('message two', 'initiative', CONSOLE_NOTIFICATION))
        await asyncio.wait_for(session, 3)
        self.assertEqual(executed, ['partially type!d'])


class PipeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pipe_partial_eof_exact_text_and_cancellation(self):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, encoding='utf-8')
        output = io.StringIO()
        terminal = AsyncLineTerminal(reader, output)
        try:
            task = asyncio.create_task(terminal.read_line('robot> '))
            os.write(write_fd, b'  caf\xc3')
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            os.write(write_fd, b'\xa9 "quote"  \nlast')
            os.close(write_fd)
            write_fd = None
            self.assertEqual(await terminal.read_line('robot> '), '  café "quote"  ')
            self.assertEqual(await terminal.read_line('robot> '), 'last')
            self.assertIsNone(await terminal.read_line('robot> '))
            self.assertNotIn('\x1b', output.getvalue())
        finally:
            if write_fd is not None:
                os.close(write_fd)
            reader.close()


class SessionRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_and_message_ready_together_preserve_command_and_eof(self):
        for value in ('exact  "command" ', None):
            with self.subTest(value=value):
                gate = asyncio.Event()
                executed = []
                reads = []
                class Console:
                    heading = 'Test'
                    prompt = '> '
                    operator_message_prefix = 'Test'
                    async def execute_async(self, line):
                        executed.append(line)
                        return '', True
                class Terminal:
                    output = ''
                    def write(self, text):
                        self.output += text
                    async def read_line(self, prompt):
                        reads.append(prompt)
                        await gate.wait()
                        return value
                class Channel:
                    async def receive(self):
                        await gate.wait()
                        return OperatorMessage('simultaneous', 'initiative', CONSOLE_NOTIFICATION)
                terminal = Terminal()
                task = asyncio.create_task(run_console_session(Console(), terminal, Channel()))
                await asyncio.sleep(0)
                gate.set()
                await asyncio.wait_for(task, 1)
                self.assertEqual(reads, ['> '])
                self.assertIn('Test: simultaneous', terminal.output)
                self.assertEqual(executed, [] if value is None else [value])

    async def test_cancellation_joins_pending_input_and_message_waits(self):
        entered = asyncio.Event()
        joined = set()
        class Console:
            heading = 'Test'
            prompt = '> '
        class Terminal:
            def write(self, text):
                pass
            async def read_line(self, prompt):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    joined.add('input')
        class Channel:
            async def receive(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    joined.add('message')
        task = asyncio.create_task(run_console_session(Console(), Terminal(), Channel()))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(joined, {'input', 'message'})
