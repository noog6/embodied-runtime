"""Async single-line terminal editing, separate from command interpretation."""

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
import logging
import os
import signal
import sys
import threading
from typing import TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session, run_in_terminal
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import History
from prompt_toolkit.input import create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import ColorDepth, create_output
from prompt_toolkit.patch_stdout import StdoutProxy

from embodied_runtime.console_style import ConsoleStyle, colour_enabled


class ConsoleTerminalError(RuntimeError):
    """Raised when cancellable terminal input is unavailable."""


class SessionHistory(History):
    """Bounded, exact command text; never persisted or shared with cognition."""

    def __init__(self, limit: int = 500) -> None:
        super().__init__()
        self._entries: deque[str] = deque(maxlen=limit)

    async def load(self) -> AsyncGenerator[str, None]:
        for entry in self.load_history_strings():
            yield entry

    def load_history_strings(self) -> Iterable[str]:
        return iter(tuple(reversed(self._entries)))

    def get_strings(self) -> list[str]:
        return list(self._entries)

    def append_string(self, string: str) -> None:
        self.store_string(string)

    def store_string(self, string: str) -> None:
        if string.strip():
            self._entries.append(string)


def _same_terminal(first: TextIO, second: TextIO) -> bool:
    try:
        return first.isatty() and second.isatty() and (
            os.ttyname(first.fileno()) == os.ttyname(second.fileno())
        )
    except (AttributeError, OSError, ValueError):
        return False


def _restore_handler_stream(handler: logging.StreamHandler, stream: TextIO) -> None:
    """Restore routing even if flushing output failed during teardown."""
    handler.acquire()
    try:
        try:
            handler.flush()
        finally:
            handler.stream = stream
    finally:
        handler.release()


class AsyncLineTerminal:
    """Selector input for pipes; prompt_toolkit's async editor for real TTYs."""

    def __init__(
        self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, *,
        no_color: bool = False, on_interrupt: Callable[[], None] | None = None,
    ) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._buffer = bytearray()
        self._eof = False
        self._session: PromptSession | None = None
        self._on_interrupt = on_interrupt
        self.style = ConsoleStyle(colour_enabled(stdout, disabled=no_color))

    @property
    def interactive(self) -> bool:
        return self._stdin.isatty() and self._stdout.isatty()

    @contextmanager
    def activate(self) -> Iterator[None]:
        """Scope editor resources and output routing to this console session."""
        if not self.interactive:
            yield
            return
        bindings = KeyBindings()

        @bindings.add('c-c')
        def interrupt(event):
            if self._on_interrupt is None:
                self.cancel_input()
            else:
                self._on_interrupt()

        # Explicit bindings keep Up/Down about logical history even when the
        # renderer wraps a command across many screen rows.
        @bindings.add('up')
        def previous(event):
            event.current_buffer.history_backward()

        @bindings.add('down')
        def following(event):
            event.current_buffer.history_forward()

        @bindings.add('enter')
        def submit(event):
            # Record after prompt_async returns, preserving spaces, duplicates,
            # and avoiding Buffer's automatic duplicate suppression.
            event.app.exit(result=event.current_buffer.text)

        @bindings.add(Keys.BracketedPaste)
        def paste(event):
            text = event.data.replace('\r\n', '\n').replace('\r', '\n')
            event.current_buffer.insert_text(text.replace('\n', ' '))

        editor_input = create_input(stdin=self._stdin)
        # prompt_toolkit restores asyncio WINCH callbacks, but not a Python
        # signal.signal handler. Preserve that case at our session boundary.
        owns_winch = threading.current_thread() is threading.main_thread()
        previous_winch = signal.getsignal(signal.SIGWINCH) if owns_winch else None
        previous_loop_winch = getattr(
            asyncio.get_running_loop(), "_signal_handlers", {},
        ).get(signal.SIGWINCH)
        try:
            output = create_output(stdout=self._stdout)
            with create_app_session(input=editor_input, output=output):
                self._session = PromptSession(
                    history=SessionHistory(), input=editor_input, output=output,
                    key_bindings=bindings, multiline=False, wrap_lines=True,
                    enable_history_search=False, enable_open_in_editor=False,
                    enable_suspend=False, include_default_pygments_style=False,
                    color_depth=None if self.style.enabled else ColorDepth.DEPTH_1_BIT,
                )
                # configure_logging duplicates stderr to survive provider dup2.
                # Retarget matching TTY handlers explicitly; leave file handlers
                # and redirected stderr alone. StdoutProxy also handles threads.
                with StdoutProxy(raw=True) as proxy, ExitStack() as routing:
                    for name in ('stdout', 'stderr'):
                        stream = getattr(sys, name)
                        if _same_terminal(stream, self._stdout):
                            routing.callback(setattr, sys, name, stream)
                            setattr(sys, name, proxy)
                    for handler in logging.getLogger().handlers:
                        if (isinstance(handler, logging.StreamHandler)
                                and not isinstance(handler, logging.FileHandler)
                                and _same_terminal(handler.stream, self._stdout)):
                            routing.callback(_restore_handler_stream, handler, handler.stream)
                            handler.setStream(proxy)
                    yield
        finally:
            self._session = None
            try:
                editor_input.close()
            finally:
                if owns_winch and previous_loop_winch is None:
                    signal.signal(signal.SIGWINCH, previous_winch)

    def cancel_input(self) -> None:
        """Discard only an unfinished draft, including its history position."""
        self._buffer.clear()
        if self._session is not None and self._session.app.is_running:
            self._session.default_buffer.reset()
            self._session.app.invalidate()

    def write(self, text: str) -> None:
        self._stdout.write(text)
        self._stdout.flush()

    async def display(self, text: str) -> None:
        if self._session is not None:
            await run_in_terminal(lambda: self.write(text))
        else:
            self.write(text)

    async def read_line(self, prompt: str) -> str | None:
        if self.interactive:
            if self._session is None:
                # Useful for standalone consumers; sessions use activate once
                # around their entire lifetime so history remains private there.
                with self.activate():
                    return await self.read_line(prompt)
            try:
                line = await self._session.prompt_async(
                    ANSI(prompt), handle_sigint=False, set_exception_handler=False,
                )
            except EOFError:
                return None
            self._session.history.append_string(line)
            return line
        self.write(prompt)
        buffered = self._take_line()
        if buffered is not ...:
            return buffered
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str | None] = loop.create_future()
        try:
            descriptor = self._stdin.fileno()
            loop.add_reader(descriptor, self._read_ready, future)
        except (AttributeError, OSError, NotImplementedError) as error:
            raise ConsoleTerminalError(
                "local console input requires asyncio selector-based stdin support"
            ) from error
        try:
            return await future
        finally:
            loop.remove_reader(descriptor)

    def _read_ready(self, future: asyncio.Future[str | None]) -> None:
        if future.done():
            return
        try:
            data = os.read(self._stdin.fileno(), 4096)
        except OSError as error:
            future.set_exception(ConsoleTerminalError(f"console input failed: {error}"))
            return
        if data:
            self._buffer.extend(data)
        else:
            self._eof = True
        line = self._take_line()
        if line is not ...:
            future.set_result(line)

    def _take_line(self) -> str | None | type(Ellipsis):
        newline = self._buffer.find(b'\n')
        if newline >= 0:
            data = bytes(self._buffer[:newline])
            del self._buffer[:newline + 1]
            return data.rstrip(b'\r').decode(self._stdin.encoding or 'utf-8', 'replace')
        if self._eof:
            if not self._buffer:
                return None
            data = bytes(self._buffer)
            self._buffer.clear()
            return data.decode(self._stdin.encoding or 'utf-8', 'replace')
        return ...
