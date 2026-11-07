"""Runtime-owned attachment, command lifetime, and transient result retention."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import time
from uuid import uuid4

from embodied_runtime.console_protocol import (
    CHUNK_CHARS, ConsoleConnectionError, IO_TIMEOUT, MAX_COMMAND,
    MAX_CONNECTIONS, REQUEST_ID, VERSION, close_writer, console_directory,
    read_frame, validate_peer, write_frame,
)
from embodied_runtime.conversation_history.continuity import delivery_attempt
from embodied_runtime.interaction import InteractionChannel

LOGGER = logging.getLogger(__name__)
RETAIN_RESULTS = 16
RETAIN_SECONDS = 15 * 60


@dataclass
class CommandState:
    id: str
    state: str = "accepted"
    response: str = ""
    runtime_request_id: str | None = None
    completed_at: float | None = None
    terminal_written: bool = False
    attempt: object | None = None

    def summary(self):
        return {"id": self.id, "state": self.state, "chars": len(self.response),
                "runtime_request_id": self.runtime_request_id,
                "terminal_written": self.terminal_written}


class _Connection:
    def __init__(self, writer):
        self.writer = writer
        self.lock = asyncio.Lock()
        self.notifications = {}

    async def send(self, message):
        # No unbounded outbound queue: at most the handler and notification pump
        # can wait here. Bound the lock wait as well as the actual socket write.
        async with asyncio.timeout(IO_TIMEOUT):
            async with self.lock:
                await write_frame(self.writer, message)


class LocalConsoleServer:
    def __init__(self, console, messages, *, directory: Path | None = None,
                 legacy: bool = False, monotonic=time.monotonic):
        self.console = console
        self.messages = messages
        self.directory = directory
        self.legacy = legacy
        self.instance_id = uuid4().hex
        self.path = None
        self._inode = None
        self._listener = None
        self._connections = set()
        self._attached = None
        self._accepted_task = None
        self._active_id = None
        self._results = OrderedDict()
        self._clock = monotonic
        self._closing = False
        self._retention_task = None

    async def start(self):
        try:
            await self._start_listener()
        except (OSError, ConsoleConnectionError) as error:
            LOGGER.error("[CONSOLE] status=setup_failed error=%s", error)
            raise ConsoleConnectionError(f"Local console setup failed: {error}; fix the directory or use --no-local-console") from error

    async def _start_listener(self):
        directory = console_directory(self.directory, create=True)
        self.path = directory / f"agent-{self.instance_id}.sock"
        if len(os.fsencode(self.path)) > 100:
            raise ConsoleConnectionError("console directory is too long for a Unix socket path")
        # A fresh instance path must not exist. asyncio's path-based server can
        # unlink sockets automatically, so bind ourselves with no unlink policy.
        import socket
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            info = self.path.lstat()
            self._inode = (info.st_dev, info.st_ino)
            self.path.chmod(0o600)
            listener.setblocking(False)
            self._listener = await asyncio.start_unix_server(self._accept, sock=listener)
        except BaseException:
            listener.close()
            self._remove_owned_endpoint()
            raise
        LOGGER.info("[CONSOLE] route=local status=listening instance=%s endpoint=%s legacy=%s",
                    self.instance_id, self.path, self.legacy)
        self._retention_task = asyncio.create_task(self._expire_results(), name="console-result-retention")

    async def _expire_results(self):
        while True:
            await asyncio.sleep(30)
            self._prune()

    def _remove_owned_endpoint(self):
        if self.path is None or self._inode is None:
            return
        try:
            info = self.path.lstat()
            import stat
            if (stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid()
                    and (info.st_dev, info.st_ino) == self._inode):
                self.path.unlink()
        except FileNotFoundError:
            pass
        self._inode = None

    def _accept(self, reader, writer):
        if self._closing or len(self._connections) >= MAX_CONNECTIONS:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer), name="local-console-client")
        self._connections.add(task)
        task.add_done_callback(self._connections.discard)

    def _prune(self):
        for key, result in list(self._results.items()):
            if result.completed_at is not None and self._clock() - result.completed_at >= RETAIN_SECONDS:
                del self._results[key]

    async def _execute(self, state, command):
        try:
            report, _ = await self.console.execute_async(command)
            state.response = report
            evidence = getattr(report, "evidence", None)
            state.runtime_request_id = evidence.id if evidence is not None else None
            state.state = "completed"
        except asyncio.CancelledError:
            state.state = "interrupted"
            state.response = "Runtime shutdown interrupted execution; effects may already have occurred."
            raise
        except Exception:
            LOGGER.exception("[CONSOLE] request=%s execution failed", state.id)
            state.state = "failed"
            state.response = "Command failed inside the runtime; inspect runtime logs. Effects may already have occurred."
        finally:
            state.completed_at = self._clock()
            self._active_id = None

    async def _notifications(self, connection):
        while True:
            message = await self.messages.receive()
            # The runtime retains only bounded in-flight receipts, never an
            # offline notification backlog. An unacknowledged write is unknown.
            if len(connection.notifications) >= 16:
                if message.delivery_attempt is not None:
                    message.delivery_attempt.finish("submission_unconfirmed", failure="transport_error")
                raise ConsoleConnectionError("console notification receipts are full")
            notification_id = uuid4().hex
            connection.notifications[notification_id] = message.delivery_attempt
            await connection.send({"type": "notification", "id": notification_id, "text": message.text})

    async def _handle(self, reader, writer):
        connection = _Connection(writer)
        pump = None
        owns_slot = False
        try:
            validate_peer(writer)
            hello = await asyncio.wait_for(read_frame(reader), IO_TIMEOUT)
            if hello["type"] != "hello" or type(hello.get("version")) is not int or hello["version"] != VERSION:
                raise ConsoleConnectionError(f"unsupported console protocol; runtime requires version {VERSION}")
            if hello.get("purpose") not in ("discover", "attach"):
                raise ConsoleConnectionError("handshake requires purpose discover or attach")
            if hello.get("instance_id", self.instance_id) != self.instance_id:
                raise ConsoleConnectionError("runtime instance changed; rediscover before attaching")
            self._prune()
            identity = {"type": "hello", "version": VERSION,
                        "agent": self.console.prompt.removesuffix("> "),
                        "name": self.console.operator_message_prefix,
                        "instance_id": self.instance_id,
                        "connection": "embedded" if self.legacy else "attached" if self._attached else "available"}
            if hello["purpose"] == "discover":
                await connection.send(identity)
                return
            if self.legacy or self._attached is not None:
                raise ConsoleConnectionError("console already attached (embedded console occupies the slot)" if self.legacy
                                             else "console already attached; only one client is supported")
            self._attached = connection
            owns_slot = True
            self.messages.set_attached(True)
            identity.update(connection="connected", recent=[r.summary() for r in self._results.values()],
                            outstanding=self._active_id, prompt=self.console.prompt)
            await connection.send(identity)
            pump = asyncio.create_task(self._notifications(connection))
            # A failed pump must wake an idle reader as well.
            # close() may wait forever to flush a full transport buffer. Abort
            # wakes the reader immediately when a slow client exhausts a bound.
            pump.add_done_callback(lambda _: writer.transport.abort())
            while True:
                message = await read_frame(reader)
                kind = message["type"]
                request_id = message.get("id")
                if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
                    raise ConsoleConnectionError("invalid console request identifier")
                self._prune()
                if kind == "command":
                    command = message.get("command")
                    if not isinstance(command, str) or len(command.encode("utf-8")) > MAX_COMMAND:
                        await connection.send({"type": "rejected", "id": request_id, "reason": f"command exceeds {MAX_COMMAND} UTF-8 bytes or is not text"})
                    elif request_id in self._results:
                        await connection.send({"type": "rejected", "id": request_id, "reason": "identifier already accepted; recover its state instead of replaying"})
                    elif self._active_id is not None:
                        await connection.send({"type": "rejected", "id": request_id, "reason": f"console busy with accepted request {self._active_id}; recover it first"})
                    else:
                        while len(self._results) >= RETAIN_RESULTS:
                            self._results.popitem(last=False)
                        state = CommandState(request_id)
                        self._results[request_id] = state
                        self._active_id = request_id
                        # Acceptance boundary: record and own the task BEFORE
                        # writing the acknowledgement. Connection cancellation
                        # never propagates to this task.
                        self._accepted_task = asyncio.create_task(self._execute(state, command), name=f"console-command-{request_id}")
                        await connection.send({"type": "accepted", **state.summary()})
                elif kind in ("status", "response"):
                    state = self._results.get(request_id)
                    if state is None:
                        await connection.send({"type": "response", "id": request_id, "state": "unknown_or_expired"})
                    elif kind == "response" and state.completed_at is not None:
                        offset = message.get("offset", 0)
                        if type(offset) is not int or not 0 <= offset <= len(state.response):
                            raise ConsoleConnectionError("invalid response offset")
                        text = state.response[offset:offset + CHUNK_CHARS]
                        if offset == 0:
                            state.attempt = delivery_attempt(state.response, channel=InteractionChannel.CONSOLE)
                        await connection.send({"type": "response", **state.summary(), "offset": offset,
                                               "text": text, "next": offset + len(text)})
                    else:
                        await connection.send({"type": "response", **state.summary()})
                elif kind == "receipt":
                    if message.get("status") != "terminal_written":
                        raise ConsoleConnectionError("unsupported console receipt")
                    if request_id in connection.notifications:
                        attempt = connection.notifications.pop(request_id)
                        if attempt is not None:
                            attempt.finish("terminal_written")
                    elif (state := self._results.get(request_id)) is not None and state.completed_at is not None:
                        state.terminal_written = True
                        if state.attempt is not None:
                            state.attempt.finish("terminal_written")
                    await connection.send({"type": "receipt", "id": request_id})
                else:
                    raise ConsoleConnectionError(f"unsupported console message type: {kind}")
        except (ConsoleConnectionError, OSError, TimeoutError, UnicodeError) as error:
            try:
                await connection.send({"type": "error", "reason": str(error) or "console handshake or write timed out"})
            except (ConsoleConnectionError, OSError, TimeoutError):
                pass
        except asyncio.CancelledError:
            raise
        finally:
            if owns_slot:
                self.messages.set_attached(False)
                self._attached = None
                for attempt in connection.notifications.values():
                    if attempt is not None:
                        attempt.finish("submission_unconfirmed", failure="transport_error")
            if pump is not None:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
            await close_writer(writer)

    async def close(self):
        self._closing = True
        if self._listener is not None:
            self._listener.close()
        for task in tuple(self._connections):
            task.cancel()
        await asyncio.gather(*tuple(self._connections), return_exceptions=True)
        if self._listener is not None:
            await self._listener.wait_closed()
        if self._accepted_task is not None:
            self._accepted_task.cancel()
            await asyncio.gather(self._accepted_task, return_exceptions=True)
        if self._retention_task is not None:
            self._retention_task.cancel()
            await asyncio.gather(self._retention_task, return_exceptions=True)
        self._remove_owned_endpoint()
