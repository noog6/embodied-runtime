"""Local, read-only projection of the running application's state."""

import asyncio
from collections.abc import Callable
import math
import os
import sys
import time
from typing import TextIO

from embodied_runtime.app import RobotApplication
from embodied_runtime.platform import PlatformSnapshot


class ConsoleTerminalError(RuntimeError):
    """Raised when cancellable terminal input is unavailable."""


class RuntimeConsole:
    """Interpret the deliberately small set of local inspection commands."""

    def __init__(
        self,
        application: RobotApplication,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._application = application
        self._monotonic = monotonic

    @property
    def prompt(self) -> str:
        return f"{self._application.profile.identifier}> "

    @property
    def heading(self) -> str:
        return f"{self._application.profile.name} Runtime Console"

    def execute(self, command: str) -> tuple[str, bool]:
        """Return report text and whether the session should terminate."""
        command = command.strip().lower()
        if not command:
            return "", False
        if command in {"quit", "exit"}:
            return "", True
        if command in {"help", "?"}:
            return self._help(), False
        if command == "status":
            return self._status(), False
        if command == "platform":
            return self._platform(), False
        if command == "hardware":
            return self._hardware(), False
        return f"Unknown command: {command}. Type 'help' for commands.", False

    @staticmethod
    def _help() -> str:
        return "\n".join(
            (
                "Commands",
                "  status    Show current runtime overview",
                "  platform  Show current host platform state",
                "  hardware  Show robot hardware backend",
                "  help      Show this help",
                "  quit      Stop the console and runtime",
                "  exit      Stop the console and runtime",
            )
        )

    def _status(self) -> str:
        summary = self._application.summary()
        runtime = "\n".join(
            (
                "Runtime",
                f"  profile:       {summary.profile_id}",
                f"  name:          {summary.profile_name}",
                f"  lifecycle:     {summary.lifecycle_status}",
            )
        )
        return f"{runtime}\n\n{self._platform()}\n\n{self._hardware()}"

    def _platform(self) -> str:
        snapshot = self._application.runtime_state.platform
        if snapshot is None:
            return "Platform\n  state:         unavailable"
        return self._render_platform(snapshot)

    def _render_platform(self, snapshot: PlatformSnapshot) -> str:
        value = lambda item: "unknown" if item is None or item == "" else str(item)
        decimal = lambda item, digits=1: (
            "unknown" if item is None or not math.isfinite(item) else f"{item:.{digits}f}"
        )
        loads = (
            "unknown"
            if snapshot.load_averages is None
            else ", ".join(decimal(load, 2) for load in snapshot.load_averages)
        )
        total = self._mib(snapshot.memory_total_bytes)
        available = self._mib(snapshot.memory_available_bytes)
        ratio = self._memory_ratio(snapshot)
        memory = (
            f"{available} / {total} MiB available ({ratio * 100:.1f}%)"
            if total is not None and available is not None and ratio is not None
            else "unknown"
        )
        age = max(0.0, self._monotonic() - snapshot.captured_monotonic)
        return "\n".join(
            (
                "Platform",
                f"  hostname:      {value(snapshot.hostname)}",
                f"  model:         {value(snapshot.model)}",
                f"  system:        {value(snapshot.system)}",
                f"  release:       {value(snapshot.release)}",
                f"  machine:       {value(snapshot.machine)}",
                f"  python:        {value(snapshot.python_version)}",
                f"  state_age_s:   {age:.1f}",
                f"  uptime:        {self._uptime(snapshot.uptime_seconds)}",
                f"  load_averages: {loads}",
                f"  cpu_temp_c:    {decimal(snapshot.cpu_temperature_celsius)}",
                f"  memory:        {memory}",
            )
        )

    def _hardware(self) -> str:
        summary = self._application.summary()
        capabilities = ", ".join(summary.capabilities) or "none"
        return "\n".join(
            (
                "Hardware",
                f"  backend:       {summary.hardware_backend}",
                f"  physical:      {str(summary.hardware_is_physical).lower()}",
                f"  capabilities:  {capabilities}",
            )
        )

    @staticmethod
    def _mib(value: int | None) -> int | None:
        return None if value is None or value < 0 else round(value / (1024 * 1024))

    @staticmethod
    def _memory_ratio(snapshot: PlatformSnapshot) -> float | None:
        total, available = snapshot.memory_total_bytes, snapshot.memory_available_bytes
        if total is None or available is None or total <= 0 or not 0 <= available <= total:
            return None
        return available / total

    @staticmethod
    def _uptime(seconds: float | None) -> str:
        if seconds is None or not math.isfinite(seconds) or seconds < 0:
            return "unknown"
        whole = int(seconds)
        days, remainder = divmod(whole, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{days}d {clock}" if days else clock


class AsyncLineTerminal:
    """Unix-selector line input without a blocked executor thread."""

    def __init__(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._buffer = bytearray()
        self._eof = False

    def write(self, text: str) -> None:
        self._stdout.write(text)
        self._stdout.flush()

    async def read_line(self, prompt: str) -> str | None:
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
        newline = self._buffer.find(b"\n")
        if newline >= 0:
            data = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            return data.rstrip(b"\r").decode(self._stdin.encoding or "utf-8", "replace")
        if self._eof:
            if not self._buffer:
                return None
            data = bytes(self._buffer)
            self._buffer.clear()
            return data.decode(self._stdin.encoding or "utf-8", "replace")
        return ...


async def run_console_session(
    console: RuntimeConsole, terminal: AsyncLineTerminal
) -> None:
    """Run a terminal session until quit, exit, or EOF."""
    terminal.write(f"\n{console.heading}\nType 'help' for commands.\n\n")
    while True:
        line = await terminal.read_line(console.prompt)
        if line is None:
            return
        report, should_exit = console.execute(line)
        if report:
            terminal.write(f"\n{report}\n\n")
        if should_exit:
            return
