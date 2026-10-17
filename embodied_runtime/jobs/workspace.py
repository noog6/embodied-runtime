"""Contained durable text and image workspaces owned by Jobs."""

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

from .images import MAX_IMAGE_BYTES, ImageMetadata, is_image, validate_image

_IMAGE_METADATA_ATTRIBUTE = "user.embodied_runtime.image"
MAX_IMAGE_PROVENANCE_BYTES = 2048

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
    kind: Literal["file", "directory", "image"]
    size_bytes: int | None
    modified_at: datetime
    content_version: str | None
    media_type: str | None = None
    width: int | None = None
    height: int | None = None


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
    modified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceWrite:
    path: str
    mode: Literal["create", "replace", "append", "upsert"]
    size_bytes: int
    content_version: str
    published: bool = True
    durability_confirmed: bool = True


@dataclass(frozen=True, slots=True)
class ImageProvenance:
    source_kind: str
    source_identity: str | None
    captured_at: datetime | None
    stored_at: datetime
    job_id: int
    job_run_id: int | None


@dataclass(frozen=True, slots=True)
class WorkspaceImage:
    path: str
    data: bytes
    metadata: ImageMetadata
    provenance: ImageProvenance | None


class JobWorkspaceStore(Protocol):
    def list_entries(self, job_id: int, directory: str = "", cursor: str | None = None) -> WorkspaceListing: ...
    def read(self, job_id: int, path: str, offset_chars: int = 0, max_chars: int = MAX_READ_CHARS) -> WorkspaceRead: ...
    def write(self, job_id: int, path: str, mode: str, content: str) -> WorkspaceWrite: ...
    def load_image(self, job_id: int, path: str) -> WorkspaceImage: ...
    def save_image(self, job_id: int, path: str, mode: str, data: bytes,
                   provenance: ImageProvenance) -> WorkspaceWrite: ...
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


def validate_artifact_path(path: str) -> None:
    """Validate a canonical Job-relative artifact path without filesystem I/O."""
    _components(path)


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
    def _read_file(directory_fd: int, name: str, *, limit: int = MAX_ARTIFACT_BYTES,
                   attributes: dict[str, bytes] | None = None,
                   header_only: bool = False) -> tuple[bytes, os.stat_result]:
        try:
            fd = os.open(name, _OPEN_FILE, dir_fd=directory_fd)
        except FileNotFoundError:
            raise WorkspaceNotFoundError("artifact not found") from None
        except OSError as error:
            raise WorkspaceUnsafeError("unsafe artifact") from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise WorkspaceUnsafeError("unsafe artifact")
            chunks: list[bytes] = []
            remaining = min(info.st_size, 8) if header_only else info.st_size
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    raise WorkspaceBackendError("artifact changed while reading")
                chunks.append(chunk)
                remaining -= len(chunk)
            current = os.fstat(fd)
            if (current.st_size != info.st_size or current.st_mtime_ns != info.st_mtime_ns
                    or current.st_nlink > 1):
                raise WorkspaceBackendError("artifact changed while reading")
            if attributes is not None:
                try:
                    raw = os.getxattr(fd, _IMAGE_METADATA_ATTRIBUTE)
                except OSError:
                    # Unreadable capture metadata cannot confer provenance.
                    pass
                else:
                    if len(raw) <= MAX_IMAGE_PROVENANCE_BYTES:
                        attributes["provenance"] = raw
            return b"".join(chunks), info
        except OSError as error:
            raise WorkspaceBackendError("artifact read failed") from error
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
                data, info = self._read_file(parent, parts[-1])
            finally:
                os.close(parent)
        finally:
            os.close(job_fd)
        if is_image(data):
            raise WorkspaceValidationError("use image operations for image artifacts")
        try:
            text = data.decode("utf-8", "strict")
        except UnicodeError as error:
            raise WorkspaceUnsafeError("artifact is not valid UTF-8") from error
        if offset_chars > len(text):
            raise WorkspaceValidationError("character offset exceeds artifact length")
        content = text[offset_chars:offset_chars + max_chars]
        next_offset = offset_chars + len(content)
        return WorkspaceRead(path, content, offset_chars, next_offset, len(text), len(data),
                             next_offset < len(text), hashlib.sha256(data).hexdigest(),
                             datetime.fromtimestamp(info.st_mtime, UTC))

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
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_IMAGE_BYTES:
                        data, _ = self._read_file(target, name, limit=MAX_IMAGE_BYTES)
                        if is_image(data):
                            self._usage(job_fd)
                            metadata = validate_image(data)
                            entries.append(WorkspaceEntry(
                                logical, name, "image", len(data), modified,
                                metadata.content_version, metadata.media_type,
                                metadata.width, metadata.height,
                            ))
                        elif len(data) <= MAX_ARTIFACT_BYTES:
                            entries.append(WorkspaceEntry(logical, name, "file", len(data), modified,
                                                          hashlib.sha256(data).hexdigest()))
                        else:
                            raise WorkspaceUnsafeError("unsafe entry prevents listing")
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

    def _usage(self, directory_fd: int, parts: tuple[str, ...] = (),
               visited: list[int] | None = None) -> tuple[int, int]:
        files = total = 0
        visited = [0] if visited is None else visited
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                name = entry.name
                visited[0] += 1
                if visited[0] > _MAX_LIST_NAMESPACE:
                    raise WorkspaceQuotaError("Workspace namespace exceeds limit")
                if self._private_temp(directory_fd, name):
                    continue
                try:
                    logical_parts = (*parts, name)
                    if len(logical_parts) > MAX_PATH_DEPTH:
                        raise WorkspaceUnsafeError("Workspace namespace exceeds depth limit")
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as error:
                    raise WorkspaceUnsafeError("unsafe entry prevents quota accounting") from error
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, _OPEN_DIR, dir_fd=directory_fd)
                    try:
                        child_files, child_total = self._usage(child, logical_parts, visited)
                    finally:
                        os.close(child)
                    files += child_files
                    total += child_total
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_IMAGE_BYTES:
                    if info.st_size > MAX_ARTIFACT_BYTES:
                        header, _ = self._read_file(directory_fd, name, limit=MAX_IMAGE_BYTES,
                                                    header_only=True)
                        if not is_image(header):
                            raise WorkspaceUnsafeError("unsafe entry prevents quota accounting")
                    files += 1
                    total += info.st_size
                else:
                    raise WorkspaceUnsafeError("unsafe entry prevents quota accounting")
                if files > MAX_WORKSPACE_FILES or total > MAX_WORKSPACE_CONTENT_BYTES:
                    raise WorkspaceQuotaError("Workspace quota exceeded")
        return files, total

    def write(self, job_id: int, path: str, mode: str, content: str) -> WorkspaceWrite:
        parts = _components(path)
        if mode not in ("create", "replace", "append", "upsert"):
            raise WorkspaceValidationError("invalid write mode")
        if type(content) is not str or "\0" in content:
            raise WorkspaceValidationError("invalid artifact text")
        try:
            supplied = content.encode("utf-8", "strict")
        except UnicodeError as error:
            raise WorkspaceValidationError("invalid artifact text") from error
        if len(supplied) > MAX_WRITE_REQUEST_BYTES:
            raise WorkspaceQuotaError("write request is too large")
        return self._write_bytes(job_id, path, mode, supplied)

    def _write_bytes(self, job_id: int, path: str, mode: str, supplied: bytes,
                     provenance: ImageProvenance | None = None) -> WorkspaceWrite:
        parts = _components(path)
        image_operation = provenance is not None
        job_creation: list[bool] = []
        created_directories: list[tuple[str, ...]] = []
        try:
            job_fd = self._open_job(
                job_id, create=(mode in ("create", "upsert")),
                created=job_creation,
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
                job_fd, parts[:-1], create=(mode in ("create", "upsert")),
                created=created_directories,
            )
            old = b""
            exists = True
            try:
                old, old_info = self._read_file(parent, parts[-1], limit=MAX_IMAGE_BYTES)
            except WorkspaceNotFoundError:
                exists = False
                old_info = None
            if mode == "create" and exists:
                raise WorkspaceConflictError("artifact already exists")
            if mode in ("replace", "append") and not exists:
                raise WorkspaceNotFoundError("artifact not found")
            if exists and is_image(old) != image_operation:
                raise WorkspaceValidationError("text and image operations are separate")
            if exists and not image_operation and len(old) > MAX_ARTIFACT_BYTES:
                raise WorkspaceUnsafeError("unsafe text artifact")
            data = old + supplied if mode == "append" else supplied
            if len(data) > (MAX_IMAGE_BYTES if image_operation else MAX_ARTIFACT_BYTES):
                raise WorkspaceQuotaError("artifact is too large")
            files, total = self._usage(job_fd)
            projected_files = files + (0 if exists else 1)
            projected_total = total - (old_info.st_size if old_info else 0) + len(data)
            if projected_files > MAX_WORKSPACE_FILES or projected_total > MAX_WORKSPACE_CONTENT_BYTES:
                raise WorkspaceQuotaError("Workspace quota exceeded")
            metadata = validate_image(data) if image_operation else None
            raw_provenance = self._encode_provenance(provenance, metadata) if image_operation else None
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
                if raw_provenance is not None:
                    os.setxattr(fd, _IMAGE_METADATA_ATTRIBUTE, raw_provenance)
                os.fsync(fd)
            finally:
                os.close(fd)
            # Revalidate the existing destination immediately before publication.
            if exists:
                current_data, current = self._read_file(parent, parts[-1], limit=MAX_IMAGE_BYTES)
                if current_data != old or old_info is None or (current.st_dev, current.st_ino, current.st_size) != (old_info.st_dev, old_info.st_ino, old_info.st_size):
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


    @staticmethod
    def _encode_provenance(provenance: ImageProvenance, metadata: ImageMetadata) -> bytes:
        def timestamp(value):
            if value is None:
                return None
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise WorkspaceValidationError("invalid image provenance time")
            return value.astimezone(UTC).isoformat()
        if (not isinstance(provenance, ImageProvenance)
                or not isinstance(provenance.stored_at, datetime)
                or type(provenance.source_kind) is not str
                or not 0 < len(provenance.source_kind) <= 64
                or (provenance.source_identity is not None and
                    (type(provenance.source_identity) is not str
                     or len(provenance.source_identity) > 120))
                or (provenance.job_run_id is not None and
                    (type(provenance.job_run_id) is not int or provenance.job_run_id <= 0))):
            raise WorkspaceValidationError("invalid image provenance")
        for value in (provenance.source_kind, provenance.source_identity):
            if value is not None and any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
                raise WorkspaceValidationError("invalid image provenance")
        _safe_job_id(provenance.job_id)
        raw = json.dumps({
            "schema": 1, "source_kind": provenance.source_kind,
            "source_identity": provenance.source_identity,
            "captured_at": timestamp(provenance.captured_at),
            "stored_at": timestamp(provenance.stored_at),
            "job_id": provenance.job_id, "job_run_id": provenance.job_run_id,
            "media_type": metadata.media_type, "width": metadata.width,
            "height": metadata.height, "content_version": metadata.content_version,
        }, separators=(",", ":"), ensure_ascii=True).encode()
        if len(raw) > MAX_IMAGE_PROVENANCE_BYTES:
            raise WorkspaceValidationError("image provenance too large")
        return raw

    @staticmethod
    def _decode_provenance(raw: bytes | None, job_id: int,
                           metadata: ImageMetadata) -> ImageProvenance | None:
        if raw is None:
            return None
        try:
            record = json.loads(raw)
            if (not isinstance(record, dict)
                    or type(record["schema"]) is not int or record["schema"] != 1
                    or type(record["job_id"]) is not int or record["job_id"] != job_id
                    or type(record["width"]) is not int or type(record["height"]) is not int
                    or record["content_version"] != metadata.content_version
                    or record["media_type"] != metadata.media_type
                    or record["width"] != metadata.width or record["height"] != metadata.height):
                return None
            provenance = ImageProvenance(
                record["source_kind"], record["source_identity"],
                datetime.fromisoformat(record["captured_at"]) if record["captured_at"] is not None else None,
                datetime.fromisoformat(record["stored_at"]), job_id, record["job_run_id"],
            )
            # Apply the same bounded runtime record validation on retrieval.
            FilesystemJobWorkspaceStore._encode_provenance(provenance, metadata)
            return provenance
        except (ValueError, TypeError, KeyError, RecursionError, WorkspaceError):
            return None

    def save_image(self, job_id: int, path: str, mode: str, data: bytes,
                   provenance: ImageProvenance) -> WorkspaceWrite:
        _components(path)
        if mode not in ("create", "replace", "upsert"):
            raise WorkspaceValidationError("invalid image write mode")
        if type(data) is not bytes or not data:
            raise WorkspaceValidationError("invalid image")
        if len(data) > MAX_IMAGE_BYTES:
            raise WorkspaceQuotaError("image encoded bytes exceed limit")
        if not isinstance(provenance, ImageProvenance) or provenance.job_id != job_id:
            raise WorkspaceValidationError("image provenance owner mismatch")
        return self._write_bytes(job_id, path, mode, data, provenance)

    def load_image(self, job_id: int, path: str) -> WorkspaceImage:
        parts = _components(path)
        job_fd = self._open_job(job_id)
        if job_fd is None:
            raise WorkspaceNotFoundError("artifact not found")
        attributes: dict[str, bytes] = {}
        try:
            self._usage(job_fd)
            parent = self._walk(job_fd, parts[:-1])
            try:
                data, _ = self._read_file(parent, parts[-1], limit=MAX_IMAGE_BYTES,
                                          attributes=attributes)
            finally:
                os.close(parent)
        finally:
            os.close(job_fd)
        metadata = validate_image(data)
        provenance = self._decode_provenance(attributes.get("provenance"), job_id, metadata)
        return WorkspaceImage(path, data, metadata, provenance)
