"""Small local development and control interface for the running application."""

import asyncio
from collections.abc import Callable
import math
import os
from pathlib import Path
import shlex
import sys
import time
from typing import TextIO

from embodied_runtime.app import RobotApplication
from embodied_runtime.cognition import CognitionError
from embodied_runtime.platform import PlatformSnapshot


class ConsoleTerminalError(RuntimeError):
    """Raised when cancellable terminal input is unavailable."""


class RuntimeConsole:
    """Interpret a deliberately small set of local development commands."""

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
        raw_parts = command.lstrip().split(maxsplit=1)
        if raw_parts and raw_parts[0].lower() == "ask":
            return "This command requires an active asynchronous console session.", False
        try:
            words = shlex.split(command)
        except ValueError as error:
            return f"Unable to parse command: {error}.", False
        if not words:
            return "", False
        vocabulary = [word.lower() for word in words]
        if vocabulary in (["quit"], ["exit"]):
            return "", True
        if vocabulary in (["help"], ["?"]):
            return self._help(), False
        if vocabulary == ["status"]:
            return self._status(), False
        if vocabulary == ["platform"]:
            return self._platform(), False
        if vocabulary == ["hardware"]:
            return self._hardware(), False
        if vocabulary == ["body"]:
            return self._body(), False
        if vocabulary == ["camera", "status"]:
            return self._camera(), False
        if vocabulary == ["presence"]:
            return self._presence(), False
        if vocabulary == ["memory"]:
            return self._memory(), False
        if vocabulary == ["goal"]:
            return self._goal(), False
        if vocabulary == ["attention"]:
            return self._attention(), False
        if vocabulary == ["goal", "clear"]:
            cleared = self._application.clear_goal()
            return "Active goal\n  cleared:       " + str(cleared).lower(), False
        if vocabulary == ["memory", "clear"]:
            cleared = self._application.working_memory.clear()
            return "\n".join((
                "Working memory",
                f"  cleared:       {cleared}",
                "  turns:         0",
            )), False
        if (
            vocabulary[:2] == ["body", "orient"]
            or vocabulary[:2] == ["simulate", "presence"]
            or vocabulary[:2] == ["camera", "capture"]
        ):
            return "This command requires an active asynchronous console session.", False
        return f"Unknown command: {words[0]}. Type 'help' for commands.", False

    async def execute_async(self, command: str) -> tuple[str, bool]:
        """Execute commands, including semantic operations that must be awaited."""
        raw_parts = command.lstrip().split(maxsplit=1)
        if raw_parts and raw_parts[0].lower() == "ask":
            message = raw_parts[1] if len(raw_parts) == 2 else ""
            if not message.strip():
                return "Usage: ask <message>.", False
            try:
                response = await self._application.request_cognition(message)
            except (CognitionError, RuntimeError, ValueError) as error:
                return f"Cognition request failed: {error}.", False
            return f"{self._application.profile.name}: {response}", False
        try:
            words = shlex.split(command)
        except ValueError as error:
            return f"Unable to parse command: {error}.", False
        lowered = [word.lower() for word in words]
        if lowered[:2] == ["camera", "capture"]:
            if len(words) != 3:
                return "Usage: camera capture <output_path>.", False
            try:
                frame = self._application.capture_camera_frame()
                Path(words[2]).write_bytes(frame.data)
            except (RuntimeError, OSError) as error:
                return f"Camera capture failed: {error}.", False
            summary = self._application.camera_summary()
            backend = "unknown" if summary is None else summary.backend
            return "\n".join(
                (
                    "Camera capture",
                    f"  backend:       {backend}",
                    f"  width:         {frame.width}",
                    f"  height:        {frame.height}",
                    f"  media_type:    {frame.media_type}",
                    f"  bytes:         {len(frame.data)}",
                    f"  output:        {words[2]}",
                    "  status:        ok",
                )
            ), False
        if lowered[:2] == ["body", "orient"]:
            if len(words) != 4:
                return "Usage: body orient <yaw> <pitch>.", False
            try:
                yaw, pitch = float(words[2]), float(words[3])
                await self._application.set_body_orientation(
                    yaw_degrees=yaw, pitch_degrees=pitch, source="console"
                )
            except (ValueError, RuntimeError) as error:
                return f"Invalid body orientation: {error}.", False
            return self._body(), False
        if lowered[:2] == ["simulate", "presence"]:
            if len(words) != 3 or lowered[2] not in {"on", "off"}:
                return "Usage: simulate presence <on|off>.", False
            present = lowered[2] == "on"
            previous = self._application.runtime_state.presence
            await self._application.observe_presence(
                present=present, source="virtual_scenario"
            )
            if previous is None or previous.present != present:
                import logging
                logging.getLogger(__name__).info(
                    "[SCENARIO] presence=%s source=virtual_scenario",
                    "present" if present else "absent",
                )
            return self._presence(), False
        return self.execute(command)

    @staticmethod
    def _help() -> str:
        return "\n".join(
            (
                "Commands",
                "  status                         Show current runtime overview",
                "  platform                       Show current host platform state",
                "  hardware                       Show robot hardware backend",
                "  body                           Show current body state",
                "  body orient <yaw> <pitch>      Set semantic body orientation",
                "  camera status                  Show configured camera resource",
                "  camera capture <output_path>   Capture one JPEG to an explicit path",
                "  presence                       Show current presence state",
                "  simulate presence <on|off>     Inject virtual presence",
                "  ask <message>                  Send one text cognition request",
                "  memory                         Show working-memory metadata",
                "  memory clear                   Clear session working memory",
                "  goal                           Show current active goal",
                "  goal clear                     Clear current active goal",
                "  attention                      Show initiative attention state",
                "  help                           Show this help",
                "  quit                           Stop the console and runtime",
                "  exit                           Stop the console and runtime",
            )
        )

    def _memory(self) -> str:
        memory = self._application.working_memory
        return "\n".join((
            "Working memory",
            f"  turns:         {len(memory)}",
            f"  capacity:      {memory.capacity}",
            f"  text_limit:    {memory.text_limit}",
        ))

    def _goal(self) -> str:
        goal = self._application.active_goal
        lines = [
            "Active goal",
            f"  state:         {'none' if goal is None else 'active'}",
        ]
        if goal is not None:
            lines.append(f"  description:   {goal.description}")
        return "\n".join(lines)

    def _attention(self) -> str:
        status = self._application.attention.status()
        return "\n".join((
            "Attention",
            f"  enabled:       {str(status.enabled).lower()}",
            f"  state:         {status.state}",
            f"  last_trigger:  {status.last_trigger or 'none'}",
            f"  last_source:   {status.last_source or 'none'}",
            f"  last_response: {status.last_response or 'unavailable'}",
        ))

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
        return (
            f"{runtime}\n\n{self._platform()}\n\n{self._hardware()}"
            f"\n\n{self._body()}\n\n{self._presence()}"
        )

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

    def _body(self) -> str:
        state = self._application.runtime_state.body
        summary = self._application.body_summary()
        if state is None or summary is None:
            return "Body\n  state:         unavailable"
        capabilities = ", ".join(summary.capabilities) or "none"
        return "\n".join(
            (
                "Body",
                f"  backend:       {summary.backend}",
                f"  physical:      {str(summary.is_physical).lower()}",
                f"  capabilities:  {capabilities}",
                f"  yaw_deg:       {state.yaw_degrees}",
                f"  pitch_deg:     {state.pitch_degrees}",
            )
        )

    def _camera(self) -> str:
        summary = self._application.camera_summary()
        if summary is None:
            return "Camera\n  state:         unavailable"
        return "\n".join(
            (
                "Camera",
                f"  backend:       {summary.backend}",
                f"  physical:      {str(summary.is_physical).lower()}",
                f"  running:       {str(summary.is_running).lower()}",
            )
        )

    def _presence(self) -> str:
        state = self._application.runtime_state.presence
        if state is None:
            status, source = "unknown", "unknown"
        else:
            status = "present" if state.present else "absent"
            source = state.source
        return "\n".join(
            ("Presence", f"  status:        {status}", f"  source:        {source}")
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
        report, should_exit = await console.execute_async(line)
        if report:
            terminal.write(f"\n{report}\n\n")
        if should_exit:
            return
