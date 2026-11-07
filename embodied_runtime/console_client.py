"""Terminal-only local attachment. Never import runtime composition or stores."""

import argparse
import asyncio
import os
from pathlib import Path
import signal
import sys
from uuid import uuid4

from embodied_runtime.console_protocol import (
    ConsoleConnectionError, IO_TIMEOUT, MAX_COMMAND, MAX_ENDPOINTS, VERSION,
    close_writer, connect, console_directory, is_detach, read_frame, write_frame,
)
from embodied_runtime.terminal import AsyncLineTerminal, ConsoleTerminalError


async def handshake(path, *, purpose="discover", instance_id=None):
    reader, writer = await connect(path)
    try:
        hello = {"type": "hello", "version": VERSION, "purpose": purpose}
        if instance_id is not None:
            hello["instance_id"] = instance_id
        await write_frame(writer, hello)
        reply = await asyncio.wait_for(read_frame(reader), IO_TIMEOUT)
        if reply["type"] == "error":
            raise ConsoleConnectionError(reply["reason"])
        if reply["type"] != "hello" or reply.get("version") != VERSION:
            raise ConsoleConnectionError("incompatible console handshake")
        if (not all(isinstance(reply.get(key), str) and reply[key]
                    for key in ('name', 'agent', 'instance_id', 'connection'))
                or reply['connection'] not in ('available', 'attached', 'embedded', 'connected')
                or (purpose == 'attach' and (
                    not isinstance(reply.get('prompt'), str)
                    or not isinstance(reply.get('recent'), list)))):
            raise ConsoleConnectionError('malformed console handshake identity')
        return reader, writer, reply
    except BaseException:
        await close_writer(writer)
        raise


async def discover(directory):
    directory = console_directory(directory)
    # Bound directory scanning as well as connection concurrency and duration.
    paths = []
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index >= 256:
                raise ConsoleConnectionError("console directory has too many entries; select an explicit --socket")
            if entry.name.startswith("agent-") and entry.name.endswith(".sock"):
                paths.append(Path(entry.path))
    if len(paths) > MAX_ENDPOINTS:
        raise ConsoleConnectionError("too many console endpoints; select an explicit --socket (stale sockets are never automatically deleted)")
    semaphore = asyncio.Semaphore(8)

    async def probe(path):
        async with semaphore:
            try:
                _, writer, identity = await handshake(path)
                await close_writer(writer)
                return path, identity, None
            except (ConsoleConnectionError, OSError, TimeoutError) as error:
                return path, None, str(error)

    return await asyncio.gather(*(probe(path) for path in sorted(paths)))


class ConsoleClient:
    def __init__(self, reader, writer, identity, terminal):
        self.reader, self.writer = reader, writer
        self.identity, self.terminal = identity, terminal
        self._request_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._waiter = None
        self.lost = asyncio.Event()
        self.failure = "Runtime connection lost; accepted work may still be running. No commands were replayed."
        self._reader_task = asyncio.create_task(self._read())

    async def _send(self, message):
        async with asyncio.timeout(IO_TIMEOUT):
            async with self._write_lock:
                await write_frame(self.writer, message)

    async def _read(self):
        try:
            while True:
                message = await read_frame(self.reader)
                if message["type"] == "notification":
                    text = self.terminal.style.operator_message(self.identity["name"], message["text"])
                    await self.terminal.display(f"\n{text}\n\n")
                    await self.receipt(message["id"])
                elif message["type"] == "receipt":
                    continue
                elif message["type"] == "error":
                    raise ConsoleConnectionError(message["reason"])
                elif self._waiter is not None and not self._waiter.done():
                    self._waiter.set_result(message)
                else:
                    raise ConsoleConnectionError("unexpected console response")
        except (ConsoleConnectionError, OSError, TimeoutError) as error:
            self.failure = f"Runtime connection lost: {error}. Accepted work may still be running; no commands were replayed."
        finally:
            self.lost.set()
            if self._waiter is not None and not self._waiter.done():
                self._waiter.set_exception(ConsoleConnectionError(self.failure))

    async def request(self, message):
        async with self._request_lock:
            if self.lost.is_set():
                raise ConsoleConnectionError(self.failure)
            self._waiter = asyncio.get_running_loop().create_future()
            try:
                await self._send(message)
                return await asyncio.wait_for(self._waiter, IO_TIMEOUT * 2)
            except TimeoutError as error:
                raise ConsoleConnectionError('console reply timed out; acceptance may be uncertain. Recover the printed request ID before repeating a command') from error
            finally:
                self._waiter = None

    async def receipt(self, request_id):
        await self._send({"type": "receipt", "id": request_id, "status": "terminal_written"})

    async def result(self, request_id, *, recovered=False):
        while True:
            state = await self.request({"type": "status", "id": request_id})
            if state.get("state") != "accepted":
                break
            await asyncio.sleep(0.2)
        if state.get("state") == "unknown_or_expired":
            await self.terminal.display(f"\nRequest {request_id}: unknown or expired outcome. Do not assume it did not execute.\n")
            return
        await self.terminal.display(f"\n{'Recovered response' if recovered else 'Response'} {request_id} ({state['state']})\n")
        offset = 0
        pending = ""
        while True:
            page = await self.request({"type": "response", "id": request_id, "offset": offset})
            if page.get("state") == "unknown_or_expired":
                raise ConsoleConnectionError("response retention expired during recovery; outcome is unknown")
            pending += page["text"]
            offset = page["next"]
            if offset == page["chars"]:
                await self.terminal.display(self.terminal.style.report(pending) + "\n\n")
                break
            # A prompt redraw can erase an unfinished output line. Keep that
            # tail across pages and end every display on a newline.
            end = pending.rfind("\n") + 1
            if end:
                await self.terminal.display(self.terminal.style.report(pending[:end]))
                pending = pending[end:]
        await self.receipt(request_id)

    async def close(self):
        self._reader_task.cancel()
        await asyncio.gather(self._reader_task, return_exceptions=True)
        await close_writer(self.writer)


async def run_attached_session(client, *, recover=None):
    terminal = client.terminal
    identity = client.identity
    terminal.write(f"Connected to {identity['name']} ({identity['agent']}) | runtime {identity['instance_id']} | connected\n")
    terminal.write("Type 'help' for runtime commands; quit, exit or EOF disconnect this terminal.\n")
    for state in identity.get("recent", []):
        terminal.write(f"Recent request {state['id']}: {state['state']}; terminal_written={str(state['terminal_written']).lower()}. Recover with --recover {state['id']}.\n")
    outstanding = recover or identity.get("outstanding")
    work = asyncio.create_task(client.result(outstanding, recovered=True)) if outstanding else None
    input_task = None
    lost_task = asyncio.create_task(client.lost.wait())
    try:
        while True:
            # Pipes keep sequential command/reply behavior. Interactive input
            # remains available during work so quit/EOF can detach immediately.
            if input_task is None and (work is None or terminal.interactive):
                prompt = terminal.style.prompt(identity["prompt"]) if terminal.interactive else ""
                input_task = asyncio.create_task(terminal.read_line(prompt))
            tasks = [t for t in (input_task, work, lost_task) if t is not None]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if lost_task in done:
                raise ConsoleConnectionError(client.failure)
            if work is not None and work in done:
                await work
                work = None
            if input_task is None or input_task not in done:
                continue
            line = input_task.result()
            input_task = None
            if line is None or is_detach(line):
                terminal.write("Disconnected; runtime continues.\n")
                return
            if not line.strip():
                continue
            if work is not None:
                terminal.write("Console work is still running. Wait for its response, or quit to detach.\n")
                continue
            if len(line.encode("utf-8")) > MAX_COMMAND:
                terminal.write(f"Command rejected locally: exceeds {MAX_COMMAND} UTF-8 bytes.\n")
                continue
            request_id = uuid4().hex
            terminal.write(f"Submitting request {request_id}\n")
            response = await client.request({"type": "command", "id": request_id, "command": line})
            if response["type"] == "rejected":
                terminal.write(f"Rejected: {response['reason']}\n")
                continue
            if response["type"] != "accepted":
                raise ConsoleConnectionError("invalid command acceptance response")
            terminal.write(f"Accepted {request_id}\n")
            work = asyncio.create_task(client.result(request_id))
    finally:
        for task in (input_task, work, lost_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in (input_task, work, lost_task) if t is not None), return_exceptions=True)


def build_parser():
    parser = argparse.ArgumentParser(description="Attach to an existing same-user local agent. Never starts a runtime. Runtime command help: type help after connecting.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--agent", help="exact agent identifier or runtime instance ID (required if discovery finds multiple agents)")
    parser.add_argument("--directory", type=Path, help="private console directory shared with runtime --local-console-directory")
    selection.add_argument("--socket", type=Path, help="explicit absolute Unix socket endpoint; bypass discovery")
    parser.add_argument("--recover", help="inspect/wait for a recent accepted request and recover its response without executing it")
    parser.add_argument("--no-color", action="store_true", help="disable terminal colours (also respects NO_COLOR)")
    return parser


async def _main(args):
    if args.socket:
        if not args.socket.is_absolute():
            raise ConsoleConnectionError("--socket must be absolute")
        path, identity = args.socket, None
    else:
        try:
            found = await discover(args.directory)
        except FileNotFoundError:
            found = []
        agents = [(path, identity) for path, identity, _ in found if identity is not None]
        if args.agent:
            agents = [(p, i) for p, i in agents if args.agent in (i["agent"], i["instance_id"])]
        if len(agents) != 1:
            for path, agent in agents:
                print(f"{agent['name']} ({agent['agent']}) runtime={agent['instance_id']} state={agent['connection']} socket={path}")
            for path, identity, error in found[:4]:
                if error:
                    print(f"Ignored unavailable/unsafe endpoint {path}: {error}", file=sys.stderr)
            raise ConsoleConnectionError("Multiple agents found; select --agent INSTANCE_ID or --socket PATH." if agents else
                                         "No matching local agent is available. Start main.py --mode run or the existing systemd service as the same user; check --directory and local_console_enabled.")
        path, identity = agents[0]
    reader, writer, hello = await handshake(path, purpose="attach", instance_id=identity["instance_id"] if identity else None)
    terminal = AsyncLineTerminal(no_color=args.no_color)
    client = ConsoleClient(reader, writer, hello, terminal)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, terminal.cancel_input)
    loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    try:
        with terminal.activate():
            await run_attached_session(client, recover=args.recover)
    finally:
        await client.close()
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except (ConsoleConnectionError, ConsoleTerminalError, OSError, TimeoutError) as error:
        print(f"Console: {str(error) or 'connection attempt timed out; inspect the local endpoint and runtime logs'}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Console disconnected; accepted work belongs to the runtime.", file=sys.stderr)
        return 0
