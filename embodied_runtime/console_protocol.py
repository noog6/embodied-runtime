"""Version 1 local-console wire and filesystem rules. Standard library only."""

import asyncio
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import struct

VERSION = 1
MAX_FRAME = 128 * 1024
MAX_COMMAND = 64 * 1024
CHUNK_CHARS = 16_000
IO_TIMEOUT = 2.0
MAX_ENDPOINTS = 64
MAX_CONNECTIONS = 16
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class ConsoleConnectionError(RuntimeError):
    """An ordinary, operator-facing local connection failure."""


def _reject_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


def console_directory(override: Path | None = None, *, create: bool = False) -> Path:
    # Deliberately independent of HOME, TMPDIR and XDG_RUNTIME_DIR. In particular,
    # SSH and systemd must resolve the same directory for the same effective UID.
    path = override if override is not None else Path(f"/tmp/embodied-runtime-{os.geteuid()}")
    if not path.is_absolute() or ".." in path.parts:
        raise ConsoleConnectionError("console directory must be an absolute path without '..'")
    # Root ownership can be mapped to the overflow UID inside a user namespace.
    # The filesystem root's owner is the system authority in either case.
    system_uid = Path('/').stat().st_uid
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (system_uid, os.geteuid()):
            raise ConsoleConnectionError(f"unsafe console parent: {parent}")
        writable = info.st_mode & 0o022
        if writable and not (info.st_uid == system_uid and info.st_mode & stat.S_ISVTX):
            raise ConsoleConnectionError(f"unsafe writable console parent: {parent}")
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ConsoleConnectionError(f"console directory must be owned by this user with mode 0700: {path}")
    return path


def validate_endpoint(path: Path) -> os.stat_result:
    console_directory(path.parent)
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ConsoleConnectionError(f"unsafe console socket (expected same-user mode 0600): {path}")
    return info


def validate_peer(writer: asyncio.StreamWriter) -> None:
    if hasattr(socket, "SO_PEERCRED"):
        peer = writer.get_extra_info("socket")
        _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.geteuid():
            raise ConsoleConnectionError("console peer belongs to a different user")


def encode_frame(message: dict) -> bytes:
    try:
        payload = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, TypeError) as error:
        raise ConsoleConnectionError("invalid JSON frame") from error
    if not 0 < len(payload) <= MAX_FRAME:
        raise ConsoleConnectionError(f"frame exceeds {MAX_FRAME} bytes")
    return struct.pack("!I", len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader) -> dict:
    # Idle attached clients may wait indefinitely. Once a frame starts, both its
    # header and body must finish within a single bounded interval.
    try:
        first = await reader.readexactly(1)
        async with asyncio.timeout(IO_TIMEOUT):
            header = first + await reader.readexactly(3)
            size, = struct.unpack("!I", header)
            if not 0 < size <= MAX_FRAME:
                raise ConsoleConnectionError(f"frame exceeds {MAX_FRAME} bytes")
            payload = await reader.readexactly(size)
        message = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            raise ValueError()
        return message
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ConsoleConnectionError("malformed JSON console frame") from error
    except (asyncio.IncompleteReadError, OSError, TimeoutError) as error:
        raise ConsoleConnectionError("console connection closed or timed out") from error


async def write_frame(writer: asyncio.StreamWriter, message: dict) -> None:
    writer.write(encode_frame(message))
    try:
        await asyncio.wait_for(writer.drain(), IO_TIMEOUT)
    except (OSError, TimeoutError) as error:
        raise ConsoleConnectionError("console write failed or timed out") from error


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), IO_TIMEOUT)
    except (OSError, TimeoutError):
        writer.transport.abort()


async def connect(path: Path):
    info = validate_endpoint(path)
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), IO_TIMEOUT)
    except (OSError, TimeoutError) as error:
        raise ConsoleConnectionError(f"cannot connect to {path}: {str(error) or 'connection timed out'}") from error
    try:
        validate_peer(writer)
        after = validate_endpoint(path)
        if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
            raise ConsoleConnectionError("console endpoint changed during connection; retry discovery")
    except BaseException:
        await close_writer(writer)
        raise
    return reader, writer


def is_detach(command: str) -> bool:
    """Only session lifecycle syntax is handled by the terminal client."""
    try:
        return [word.lower() for word in shlex.split(command)] in (["quit"], ["exit"])
    except ValueError:
        return False
