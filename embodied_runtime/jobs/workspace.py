"""Contained durable text workspaces owned by Jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import base64
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import re
from typing import Literal, Protocol

MAX_LOGICAL_PATH_BYTES = 240
MAX_COMPONENT_BYTES = 100
MAX_PATH_DEPTH = 8
MAX_READ_CHARS = 8_000
MAX_WRITE_REQUEST_BYTES = 16 * 1024
MAX_ARTIFACT_BYTES = 256 * 1024
MAX_LIST_ENTRIES = 100
MAX_WORKSPACE_FILES = 128
MAX_WORKSPACE_CONTENT_BYTES = 8 * 1024 * 1024
_TEMP_PREFIX = ".workspace-tmp-"
_TEMP_NAME = re.compile(r"\.workspace-tmp-[0-9a-f]{32}\Z")
_MAX_LIST_NAMESPACE = MAX_WORKSPACE_FILES * MAX_PATH_DEPTH
_RENAME_NOREPLACE = 1
_OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# Nonblocking is essential while opening an untrusted FIFO leaf: verification
# happens on the opened descriptor, so the open itself must not wait for a peer.
_OPEN_FILE = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC


class WorkspaceError(RuntimeError):
    """A bounded, non-path-disclosing Workspace failure."""


class WorkspaceValidationError(WorkspaceError, ValueError):
    pass


class WorkspaceQuotaError(WorkspaceError):
    """A valid operation exceeds a fixed Workspace storage ceiling."""


class WorkspaceNotFoundError(WorkspaceError, FileNotFoundError):
    pass


class WorkspaceConflictError(WorkspaceError, FileExistsError):
    pass


class WorkspaceUnsafeError(WorkspaceError):
    pass


class WorkspaceBackendError(WorkspaceError):
    pass


class WorkspaceDurabilityError(WorkspaceBackendError):
    """Publication happened, but directory durability could not be confirmed."""

    published = True
    durability_confirmed = False


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    path: str
    name: str
    kind: Literal["file", "directory"]
    size_bytes: int | None
    modified_at: datetime
    content_version: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceListing:
    directory: str
    entries: tuple[WorkspaceEntry, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceRead:
    path: str
    content: str
    offset_chars: int
    next_offset_chars: int
    total_chars: int
    size_bytes: int
    truncated: bool
    content_version: str


@dataclass(frozen=True, slots=True)
class WorkspaceWrite:
    path: str
    mode: Literal["create", "replace", "append"]
    size_bytes: int
    content_version: str
    published: bool = True
    durability_confirmed: bool = True


class JobWorkspaceStore(Protocol):
    def list_entries(self, job_id: int, directory: str = "", cursor: str | None = None) -> WorkspaceListing: ...
    def read(self, job_id: int, path: str, offset_chars: int = 0, max_chars: int = MAX_READ_CHARS) -> WorkspaceRead: ...
    def write(self, job_id: int, path: str, mode: str, content: str) -> WorkspaceWrite: ...
    def close(self) -> None: ...


def workspace_root_for_database(database_path: Path) -> Path:
    """Derive the sibling Workspace root from the Jobs database stem."""
    return database_path.with_name(f"{database_path.stem}-workspaces")


def _components(path: str, *, directory: bool = False) -> tuple[str, ...]:
    if type(path) is not str or (not path and not directory):
        raise WorkspaceValidationError("invalid logical path")
    if not path:
        return ()
    if path.startswith(("/", "\\")) or "\\" in path or "\0" in path:
        raise WorkspaceValidationError("invalid logical path")
    try:
        encoded = path.encode("utf-8", "strict")
    except UnicodeError as error:
        raise WorkspaceValidationError("invalid logical path") from error
    if len(encoded) > MAX_LOGICAL_PATH_BYTES:
        raise WorkspaceValidationError("logical path is too long")
    parts = tuple(path.split("/"))
    if len(parts) > MAX_PATH_DEPTH or any(not part or part in (".", "..") for part in parts):
        raise WorkspaceValidationError("invalid logical path")
    if len(parts[0]) >= 2 and parts[0][0].isalpha() and parts[0][1] == ":":
        raise WorkspaceValidationError("invalid logical path")
    for part in parts:
        raw = part.encode("utf-8")
        if len(raw) > MAX_COMPONENT_BYTES or part.startswith(_TEMP_PREFIX):
            raise WorkspaceValidationError("invalid logical path component")
        if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in part):
            raise WorkspaceValidationError("invalid logical path component")
    return parts


def _safe_job_id(job_id: int) -> str:
    if type(job_id) is not int or job_id <= 0:
        raise WorkspaceValidationError("invalid Job ID")
    return f"JOB{job_id}"


class FilesystemJobWorkspaceStore:
    """Linux descriptor-relative, no-follow Workspace implementation."""

    def __init__(self, root: Path) -> None:
        required = ("O_DIRECTORY", "O_NOFOLLOW", "supports_dir_fd")
        if any(not hasattr(os, name) for name in required[:2]) or not os.supports_dir_fd:
            raise WorkspaceBackendError("required filesystem containment is unavailable")
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            self._renameat2 = libc.renameat2
        except AttributeError as error:
            raise WorkspaceBackendError(
                "atomic no-clobber publication is unavailable"
            ) from error
        self._renameat2.argtypes = (
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint,
        )
        self._renameat2.restype = ctypes.c_int
        self._closed = False
        try:
            try:
                info = os.lstat(root)
            except FileNotFoundError:
                root.mkdir(mode=0o700, parents=False)
                info = os.lstat(root)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise WorkspaceUnsafeError("Workspace root is unsafe")
            self._root_fd = os.open(root, _OPEN_DIR)
            opened = os.fstat(self._root_fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                os.close(self._root_fd)
                raise WorkspaceUnsafeError("Workspace root changed during initialization")
        except WorkspaceError:
            raise
        except OSError as error:
            raise WorkspaceBackendError("Workspace root initialization failed") from error

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._root_fd)

    def _check_open(self) -> None:
        if self._closed:
            raise WorkspaceBackendError("Workspace store is closed")

    def _open_job(self, job_id: int, *, create: bool = False,
                  created: list[bool] | None = None) -> int | None:
        self._check_open()
        name = _safe_job_id(job_id)
        try:
            return os.open(name, _OPEN_DIR, dir_fd=self._root_fd)
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir(name, 0o700, dir_fd=self._root_fd)
                if created is not None:
                    created.append(True)
                os.fsync(self._root_fd)
                return os.open(name, _OPEN_DIR, dir_fd=self._root_fd)
            except FileExistsError:
                raise WorkspaceUnsafeError("Job Workspace is unsafe") from None
        except OSError as error:
            raise WorkspaceUnsafeError("Job Workspace is unsafe") from error

    def _walk(self, start_fd: int, parts: tuple[str, ...], *, create: bool = False,
              created: list[tuple[str, ...]] | None = None) -> int:
        current = os.dup(start_fd)
        walked: list[str] = []
        try:
            for component in parts:
                try:
                    child = os.open(component, _OPEN_DIR, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise WorkspaceNotFoundError("Workspace directory not found") from None
                    os.mkdir(component, 0o700, dir_fd=current)
                    if created is not None:
                        created.append(tuple((*walked, component)))
                    os.fsync(current)
                    child = os.open(component, _OPEN_DIR, dir_fd=current)
                except OSError as error:
                    raise WorkspaceUnsafeError("unsafe Workspace directory") from error
                os.close(current)
                current = child
                walked.append(component)
            return current
        except BaseException:
            os.close(current)
            raise

    @staticmethod
    def _private_temp(directory_fd: int, name: str) -> bool:
        """Recognize only exact, safe runtime temporary files."""
        if _TEMP_NAME.fullmatch(name) is None:
            return False
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise WorkspaceUnsafeError("unsafe internal temporary entry") from error
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkspaceUnsafeError("unsafe internal temporary entry")
        return True

    def _publish_create(self, directory_fd: int, temporary: str,
                        destination: str) -> None:
        """Atomically move a temporary file into place without replacement."""
        result = self._renameat2(
            directory_fd, os.fsencode(temporary), directory_fd,
            os.fsencode(destination), _RENAME_NOREPLACE,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise WorkspaceConflictError("artifact already exists")
        if error_number in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise WorkspaceBackendError(
                "atomic no-clobber publication is unavailable"
            )
        raise WorkspaceBackendError("Workspace publication failed")

    def _cleanup_created_directories(
        self, job_id: int, directories: list[tuple[str, ...]], job_created: bool,
    ) -> None:
        """Best-effort removal of only empty directories made by this attempt."""
        for parts in reversed(directories):
            job_fd = self._open_job(job_id)
            if job_fd is None:
                break
            parent = -1
            try:
                parent = self._walk(job_fd, parts[:-1])
                os.rmdir(parts[-1], dir_fd=parent)
            except (OSError, WorkspaceError):
                pass
            finally:
                if parent >= 0:
                    os.close(parent)
                os.close(job_fd)
        if job_created:
            try:
                os.rmdir(_safe_job_id(job_id), dir_fd=self._root_fd)
                os.fsync(self._root_fd)
            except OSError:
                pass

    @staticmethod
    def _read_file(directory_fd: int, name: str) -> tuple[bytes, os.stat_result]:
        try:
            fd = os.open(name, _OPEN_FILE, dir_fd=directory_fd)
        except FileNotFoundError:
            raise WorkspaceNotFoundError("artifact not found") from None
        except OSError as error:
            raise WorkspaceUnsafeError("unsafe artifact") from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_ARTIFACT_BYTES:
                raise WorkspaceUnsafeError("unsafe artifact")
            chunks: list[bytes] = []
            remaining = info.st_size
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    raise WorkspaceBackendError("artifact changed while reading")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.fstat(fd).st_size != info.st_size:
                raise WorkspaceBackendError("artifact changed while reading")
            return b"".join(chunks), info
        finally:
            os.close(fd)

    def read(self, job_id: int, path: str, offset_chars: int = 0, max_chars: int = MAX_READ_CHARS) -> WorkspaceRead:
        parts = _components(path)
        if type(offset_chars) is not int or offset_chars < 0 or type(max_chars) is not int or not 0 < max_chars <= MAX_READ_CHARS:
            raise WorkspaceValidationError("invalid read range")
        job_fd = self._open_job(job_id)
        if job_fd is None:
            raise WorkspaceNotFoundError("artifact not found")
        try:
            parent = self._walk(job_fd, parts[:-1])
            try:
                data, _ = self._read_file(parent, parts[-1])
            finally:
                os.close(parent)
        finally:
            os.close(job_fd)
        try:
            text = data.decode("utf-8", "strict")
        except UnicodeError as error:
            raise WorkspaceUnsafeError("artifact is not valid UTF-8") from error
        if offset_chars > len(text):
            raise WorkspaceValidationError("character offset exceeds artifact length")
        content = text[offset_chars:offset_chars + max_chars]
        next_offset = offset_chars + len(content)
        return WorkspaceRead(path, content, offset_chars, next_offset, len(text), len(data),
                             next_offset < len(text), hashlib.sha256(data).hexdigest())

    def list_entries(self, job_id: int, directory: str = "", cursor: str | None = None) -> WorkspaceListing:
        parts = _components(directory, directory=True)
        start = self._decode_cursor(cursor, job_id, directory)
        job_fd = self._open_job(job_id)
        if job_fd is None:
            if cursor is not None:
                raise WorkspaceValidationError("invalid listing cursor")
            return WorkspaceListing(directory, (), None)
        try:
            target = self._walk(job_fd, parts)
            try:
                names: list[str] = []
                for name in os.listdir(target):
                    if self._private_temp(target, name):
                        continue
                    try:
                        _components("/".join((*parts, name)))
                    except WorkspaceValidationError as error:
                        raise WorkspaceUnsafeError(
                            "unsafe entry prevents listing"
                        ) from error
                    names.append(name)
                    if len(names) > _MAX_LIST_NAMESPACE:
                        raise WorkspaceUnsafeError("Workspace directory is too large")
                names.sort(key=lambda value: value.encode("utf-8"))
                if cursor is not None and start >= len(names):
                    raise WorkspaceValidationError("invalid listing cursor")
                entries: list[WorkspaceEntry] = []
                page_names = names[start:start + MAX_LIST_ENTRIES]
                for name in page_names:
                    try:
                        info = os.stat(name, dir_fd=target, follow_symlinks=False)
                    except OSError as error:
                        raise WorkspaceUnsafeError("unsafe entry prevents listing") from error
                    logical = "/".join((*parts, name))
                    modified = datetime.fromtimestamp(info.st_mtime, UTC)
                    if stat.S_ISDIR(info.st_mode):
                        entries.append(WorkspaceEntry(logical, name, "directory", None, modified, None))
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_ARTIFACT_BYTES:
                        data, _ = self._read_file(target, name)
                        entries.append(WorkspaceEntry(logical, name, "file", info.st_size, modified,
                                                      hashlib.sha256(data).hexdigest()))
                    else:
                        raise WorkspaceUnsafeError("unsafe entry prevents listing")
            finally:
                os.close(target)
        finally:
            os.close(job_fd)
        page = tuple(entries)
        following = start + len(page)
        next_cursor = self._encode_cursor(job_id, directory, following) if following < len(names) else None
        return WorkspaceListing(directory, page, next_cursor)

    @staticmethod
    def _encode_cursor(job_id: int, directory: str, offset: int) -> str:
        raw = json.dumps([job_id, directory, offset], ensure_ascii=False, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None, job_id: int, directory: str) -> int:
        if cursor is None:
            return 0
        try:
            if type(cursor) is not str or len(cursor) > 512:
                raise ValueError
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            bound_job, bound_directory, offset = json.loads(raw)
            if bound_job != job_id or bound_directory != directory or type(offset) is not int or offset < 0:
                raise ValueError
            return offset
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as error:
            raise WorkspaceValidationError("invalid listing cursor") from error

    def _usage(self, directory_fd: int) -> tuple[int, int]:
        files = total = 0
        for name in os.listdir(directory_fd):
            if self._private_temp(directory_fd, name):
                continue
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as error:
                raise WorkspaceUnsafeError("unsafe entry prevents quota accounting") from error
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, _OPEN_DIR, dir_fd=directory_fd)
                try:
                    child_files, child_total = self._usage(child)
                finally:
                    os.close(child)
                files += child_files
                total += child_total
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_ARTIFACT_BYTES:
                files += 1
                total += info.st_size
            else:
                raise WorkspaceUnsafeError("unsafe entry prevents quota accounting")
        return files, total

    def write(self, job_id: int, path: str, mode: str, content: str) -> WorkspaceWrite:
        parts = _components(path)
        if mode not in ("create", "replace", "append"):
            raise WorkspaceValidationError("invalid write mode")
        if type(content) is not str or "\0" in content:
            raise WorkspaceValidationError("invalid artifact text")
        try:
            supplied = content.encode("utf-8", "strict")
        except UnicodeError as error:
            raise WorkspaceValidationError("invalid artifact text") from error
        if len(supplied) > MAX_WRITE_REQUEST_BYTES:
            raise WorkspaceQuotaError("write request is too large")
        job_creation: list[bool] = []
        created_directories: list[tuple[str, ...]] = []
        try:
            job_fd = self._open_job(
                job_id, create=(mode == "create"), created=job_creation,
            )
        except BaseException:
            if job_creation:
                self._cleanup_created_directories(job_id, [], True)
            raise
        if job_fd is None:
            raise WorkspaceNotFoundError("artifact not found")
        temp_name: str | None = None
        parent = -1
        published = False
        try:
            parent = self._walk(
                job_fd, parts[:-1], create=(mode == "create"),
                created=created_directories,
            )
            old = b""
            exists = True
            try:
                old, old_info = self._read_file(parent, parts[-1])
            except WorkspaceNotFoundError:
                exists = False
                old_info = None
            if mode == "create" and exists:
                raise WorkspaceConflictError("artifact already exists")
            if mode != "create" and not exists:
                raise WorkspaceNotFoundError("artifact not found")
            data = old + supplied if mode == "append" else supplied
            if len(data) > MAX_ARTIFACT_BYTES:
                raise WorkspaceQuotaError("artifact is too large")
            files, total = self._usage(job_fd)
            projected_files = files + (0 if exists else 1)
            projected_total = total - (old_info.st_size if old_info else 0) + len(data)
            if projected_files > MAX_WORKSPACE_FILES or projected_total > MAX_WORKSPACE_CONTENT_BYTES:
                raise WorkspaceQuotaError("Workspace quota exceeded")
            temp_name = _TEMP_PREFIX + secrets.token_hex(16)
            fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise WorkspaceBackendError("short Workspace write")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            # Revalidate the existing destination immediately before publication.
            if mode != "create":
                _, current = self._read_file(parent, parts[-1])
                if old_info is None or (current.st_dev, current.st_ino, current.st_size) != (old_info.st_dev, old_info.st_ino, old_info.st_size):
                    raise WorkspaceConflictError("artifact changed during write")
                os.replace(temp_name, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                temp_name = None
            else:
                self._publish_create(parent, temp_name, parts[-1])
                temp_name = None
            published = True
            try:
                os.fsync(parent)
            except OSError as error:
                raise WorkspaceDurabilityError("artifact published; durability unconfirmed") from error
            return WorkspaceWrite(path, mode, len(data), hashlib.sha256(data).hexdigest())
        except WorkspaceError:
            raise
        except OSError as error:
            raise WorkspaceBackendError("Workspace filesystem operation failed") from error
        finally:
            if temp_name is not None and parent >= 0:
                try:
                    os.unlink(temp_name, dir_fd=parent)
                except OSError:
                    pass
            if parent >= 0:
                os.close(parent)
            os.close(job_fd)
            if not published:
                self._cleanup_created_directories(
                    job_id, created_directories, bool(job_creation),
                )
