"""Small, local, append-only records for runtime executions."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_HISTORY_ROOT = Path("data/runs")
_RUN_DIRECTORY = re.compile(r"R([1-9][0-9]*)\Z")
MAX_METADATA_BYTES = 64 * 1024
MAX_LISTED_RUNS = 20
MAX_GREP_MATCHES = 50
MAX_GREP_QUERY_LENGTH = 256
MAX_COGNITION_RUNS = 5
MAX_COGNITION_MATCHES = 20
MAX_COGNITION_LINE_CHARS = 800
MAX_DAILY_RUNS = 20
_CONTENT_FIELDS = (
    "text=", "message=", "purpose=", "description=", "summary=", "evidence=",
    "focus=", "query=", "prompt=", "utterance=", "response=", "heard=", "words=",
)
_STRUCTURED_LOG_RECORD = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}"
    r"(?:Z|[+-]\d{2}:\d{2}) \[([A-Z][A-Z0-9_-]*)\](?: |$)"
)


class RunMetadataError(ValueError):
    """Persisted metadata is not a valid schema-v1 record."""


class UnsupportedRunSchema(RunMetadataError):
    """Persisted metadata declares a schema this runtime cannot read."""

    def __init__(self, version: object) -> None:
        super().__init__(f"unsupported schema version {version}")
        self.version = version


class RunDataUnavailable(OSError):
    """A historical run artifact cannot safely be read."""


@dataclass(frozen=True)
class RunRecord:
    """Validated, read-only presentation model for schema-v1 metadata."""

    schema_version: int
    run_id: str
    run_number: int
    started_at: str
    ended_at: str | None
    status: str
    exit_code: int | None
    profile: str
    hardware: str
    config_source: str | None

    @property
    def duration(self) -> str:
        if self.ended_at is None:
            return "-"
        seconds = int((datetime.fromisoformat(self.ended_at) -
                       datetime.fromisoformat(self.started_at)).total_seconds())
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{days}d {clock}" if days else clock


def canonical_run_id(identity: str) -> str | None:
    """Return a canonical ID for one exact, case-insensitive run identity."""
    match = re.fullmatch(r"[Rr]([1-9][0-9]*)", identity)
    return f"R{int(match.group(1))}" if match is not None else None


def discover_run_ids(root: Path) -> tuple[tuple[str, ...], int]:
    """Return at most the newest 20 safe direct run directories and older count."""
    try:
        children = root.iterdir()
        numbered = sorted(
            ((int(match.group(1)), child.name) for child in children
             if (match := _RUN_DIRECTORY.fullmatch(child.name)) is not None
             and not child.is_symlink() and child.is_dir()),
            reverse=True,
        )
    except OSError:
        return (), 0
    return (tuple(name for _, name in numbered[:MAX_LISTED_RUNS]),
            max(0, len(numbered) - MAX_LISTED_RUNS))


def _safe_run_directory(root: Path, run_id: str) -> Path | None:
    directory = root / run_id
    try:
        return directory if not directory.is_symlink() and directory.is_dir() else None
    except OSError:
        return None


def read_run_record(root: Path, run_id: str) -> RunRecord:
    """Read and validate one bounded schema-v1 record without following links."""
    if canonical_run_id(run_id) != run_id:
        raise FileNotFoundError(run_id)
    directory = _safe_run_directory(root, run_id)
    if directory is None:
        raise FileNotFoundError(run_id)
    path = directory / "run.json"
    try:
        if (path.is_symlink() or not path.is_file()
                or path.stat().st_size > MAX_METADATA_BYTES):
            raise RunDataUnavailable(run_id)
        with path.open("r", encoding="utf-8") as source:
            raw = source.read(MAX_METADATA_BYTES + 1)
    except (OSError, UnicodeError) as error:
        raise RunDataUnavailable(run_id) from error
    if len(raw.encode("utf-8")) > MAX_METADATA_BYTES:
        raise RunDataUnavailable(run_id)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise RunMetadataError(run_id) from error
    if not isinstance(data, dict):
        raise RunMetadataError(run_id)
    version = data.get("schema_version")
    if type(version) is not int:
        raise RunMetadataError(run_id)
    if version != 1:
        raise UnsupportedRunSchema(version)
    expected = int(run_id[1:])
    scalar_types = (
        type(data.get("run_id")) is str,
        type(data.get("run_number")) is int,
        type(data.get("started_at")) is str,
        data.get("ended_at") is None or type(data.get("ended_at")) is str,
        type(data.get("status")) is str,
        data.get("exit_code") is None or type(data.get("exit_code")) is int,
        type(data.get("profile")) is str,
        type(data.get("hardware")) is str,
        data.get("config_source") is None or type(data.get("config_source")) is str,
    )
    if not all(scalar_types) or data["run_id"] != run_id or data["run_number"] != expected:
        raise RunMetadataError(run_id)
    try:
        started = datetime.fromisoformat(data["started_at"])
        ended = datetime.fromisoformat(data["ended_at"]) if data["ended_at"] else None
        if started.tzinfo is None or (ended is not None and
                                     (ended.tzinfo is None or ended < started)):
            raise ValueError
    except ValueError as error:
        raise RunMetadataError(run_id) from error
    status, exit_code = data["status"], data["exit_code"]
    valid_final = (
        status == "started" and ended is None and exit_code is None
        or status == "completed" and ended is not None and exit_code == 0
        or status == "interrupted" and ended is not None and exit_code == 130
        or status == "failed" and ended is not None and exit_code not in (None, 0, 130)
    )
    if not valid_final:
        raise RunMetadataError(run_id)
    return RunRecord(**{field: data[field] for field in RunRecord.__dataclass_fields__})


def grep_run_log(root: Path, run_id: str, query: str) -> tuple[tuple[tuple[int, str], ...], bool]:
    """Search one safe log line-by-line for a bounded literal query."""
    if canonical_run_id(run_id) != run_id:
        raise FileNotFoundError(run_id)
    directory = _safe_run_directory(root, run_id)
    if directory is None:
        raise FileNotFoundError(run_id)
    path = directory / "runtime.log"
    try:
        if path.is_symlink() or not path.is_file():
            raise RunDataUnavailable(run_id)
        matches: list[tuple[int, str]] = []
        needle = query.casefold()
        with path.open("r", encoding="utf-8", errors="replace") as source:
            for line_number, line in enumerate(source, 1):
                line = line.rstrip("\r\n")
                if needle in line.casefold():
                    if len(matches) == MAX_GREP_MATCHES:
                        return tuple(matches), True
                    matches.append((line_number, line))
    except OSError as error:
        raise RunDataUnavailable(run_id) from error
    return tuple(matches), False


def _record_dict(record: RunRecord) -> dict[str, object]:
    return {
        "run_id": record.run_id, "status": record.status,
        "started_at": record.started_at, "ended_at": record.ended_at,
        "exit_code": record.exit_code, "duration": record.duration,
        "profile": record.profile, "hardware": record.hardware,
        "config_source": record.config_source,
    }


class RunHistoryEvidenceReader:
    """Read-only, bounded, model-safe projection of persisted run evidence."""

    def __init__(self, root: Path, current_run_id: str | None = None, *,
                 timezone_name: str = "UTC",
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._root = root
        self._timezone_name = timezone_name
        self._timezone = ZoneInfo(timezone_name)
        self._clock = clock
        self.current_run_id = (
            current_run_id if current_run_id is not None
            and canonical_run_id(current_run_id) == current_run_id else None
        )

    def inspect(self, operation: object, run: object = None,
                query: object = None) -> dict[str, object]:
        if operation not in ("recent", "overview", "search"):
            return self._rejected("invalid_tool_arguments")
        if operation == "recent":
            if run is not None or query is not None:
                return self._rejected("invalid_tool_arguments")
            return self._recent()
        if type(run) is not str or (operation == "overview" and query is not None):
            return self._rejected("invalid_tool_arguments")
        if operation == "search" and (type(query) is not str or not query.strip()
                                      or len(query) > MAX_GREP_QUERY_LENGTH):
            return self._rejected("invalid_tool_arguments")
        if run == "previous_day":
            return self._previous_day(operation, query)
        resolved, reason = self._resolve(run)
        if resolved is None:
            return self._rejected(reason or "invalid_run_selector")
        record, reason = self._record(resolved)
        if record is None:
            return self._rejected(reason, run_id=resolved)
        if operation == "overview":
            evidence, reason = self._overview_log(resolved)
            if evidence is None:
                return self._rejected(reason, run_id=resolved)
            return {"status": "applied", "operation": operation,
                    "selector": run, "run": _record_dict(record),
                    "evidence": evidence}
        matches, truncated, reason = self._search_log(resolved, query)
        if matches is None:
            return self._rejected(reason, run_id=resolved)
        return {"status": "applied", "operation": operation, "run_id": resolved,
                "query": query, "matches": matches, "truncated": truncated}

    @staticmethod
    def _rejected(reason: str, **extra: object) -> dict[str, object]:
        return {"status": "rejected", "reason": reason, **extra}

    def _safe_ids(self) -> list[str]:
        try:
            numbered = []
            for child in self._root.iterdir():
                match = _RUN_DIRECTORY.fullmatch(child.name)
                if match and not child.is_symlink() and child.is_dir():
                    numbered.append((int(match.group(1)), child.name))
            return [name for _, name in sorted(numbered, reverse=True)]
        except OSError:
            return []

    def _resolve(self, selector: str) -> tuple[str | None, str | None]:
        if selector == "current":
            return ((self.current_run_id, None) if self.current_run_id else
                    (None, "current_run_unavailable"))
        if selector == "previous":
            if self.current_run_id is None:
                return None, "previous_run_unavailable"
            current = int(self.current_run_id[1:])
            lower = [item for item in self._safe_ids() if int(item[1:]) < current]
            return ((lower[0], None) if lower else
                    (None, "previous_run_unavailable"))
        canonical = canonical_run_id(selector)
        return ((canonical, None) if canonical else (None, "invalid_run_selector"))

    def _record(self, run_id: str) -> tuple[RunRecord | None, str]:
        try:
            return read_run_record(self._root, run_id), ""
        except FileNotFoundError:
            return None, "run_not_found"
        except UnsupportedRunSchema:
            return None, "unsupported_schema"
        except RunMetadataError:
            return None, "metadata_invalid"
        except RunDataUnavailable:
            return None, "metadata_unavailable"

    def _recent(self) -> dict[str, object]:
        runs: list[dict[str, object]] = []
        for run_id in self._safe_ids()[:MAX_COGNITION_RUNS]:
            record, reason = self._record(run_id)
            runs.append(_record_dict(record) if record else
                        {"run_id": run_id, "status": "unavailable", "reason": reason})
        return {"status": "applied", "operation": "recent",
                "current_run_id": self.current_run_id, "runs": runs}

    @staticmethod
    def _safe_line(line: str) -> bool:
        folded = line.casefold()
        return (_STRUCTURED_LOG_RECORD.match(line) is not None
                and not any(marker in folded for marker in _CONTENT_FIELDS))

    @staticmethod
    def _bounded_line(line_number: int, line: str) -> dict[str, object]:
        if len(line) <= MAX_COGNITION_LINE_CHARS:
            return {"line_number": line_number, "text": line}
        suffix = "…<truncated>"
        return {"line_number": line_number,
                "text": line[:MAX_COGNITION_LINE_CHARS - len(suffix)] + suffix,
                "truncated": True}

    def _complete_lines(self, run_id: str):
        directory = _safe_run_directory(self._root, run_id)
        if directory is None:
            raise FileNotFoundError(run_id)
        path = directory / "runtime.log"
        if path.is_symlink() or not path.is_file():
            raise RunDataUnavailable(run_id)
        with path.open("r", encoding="utf-8", errors="replace", newline="") as source:
            for number, line in enumerate(source, 1):
                if not line.endswith(("\n", "\r")):
                    break
                yield number, line.rstrip("\r\n")

    def _overview_log(self, run_id: str):
        from collections import Counter, deque
        categories: Counter[str] = Counter()
        first: list[dict[str, object]] = []
        last = deque(maxlen=16)
        safe_count = 0
        try:
            for number, line in self._complete_lines(run_id):
                if not self._safe_line(line):
                    continue
                safe_count += 1
                match = _STRUCTURED_LOG_RECORD.match(line)
                if match:
                    categories[match.group(1)] += 1
                item = self._bounded_line(number, line)
                if len(first) < 8:
                    first.append(item)
                last.append(item)
        except (OSError, FileNotFoundError):
            return None, "runtime_log_unavailable"
        by_number = {item["line_number"]: item for item in (*first, *last)}
        lines = [by_number[key] for key in sorted(by_number)]
        return {"category_counts": dict(sorted(categories.items())), "lines": lines,
                "truncated": safe_count > len(lines)}, ""

    def _search_log(self, run_id: str, query: str):
        matches: list[dict[str, object]] = []
        needle = query.casefold()
        try:
            for number, line in self._complete_lines(run_id):
                if not self._safe_line(line) or needle not in line.casefold():
                    continue
                if len(matches) == MAX_COGNITION_MATCHES:
                    return matches, True, ""
                matches.append(self._bounded_line(number, line))
        except (OSError, FileNotFoundError):
            return None, False, "runtime_log_unavailable"
        return matches, False, ""

    def _previous_day(self, operation: str, query: object) -> dict[str, object]:
        """Aggregate safe completed log records by local line timestamp."""
        local_now = self._clock().astimezone(self._timezone)
        calendar_date = local_now.date() - timedelta(days=1)
        categories: dict[str, int] = {}
        first: list[dict[str, object]] = []
        last: list[dict[str, object]] = []
        matches: list[dict[str, object]] = []
        run_ids: list[str] = []
        safe_count = 0
        truncated = False
        ids = list(reversed(self._safe_ids()))
        if len(ids) > MAX_DAILY_RUNS:
            truncated = True
            ids = ids[-MAX_DAILY_RUNS:]
        needle = query.casefold() if isinstance(query, str) else None
        for run_id in ids:
            represented = False
            try:
                lines = self._complete_lines(run_id)
                for number, line in lines:
                    if not self._safe_line(line):
                        continue
                    try:
                        timestamp = datetime.fromisoformat(line.split(" ", 1)[0].replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if timestamp.astimezone(self._timezone).date() != calendar_date:
                        continue
                    represented = True
                    safe_count += 1
                    item = {"run_id": run_id, **self._bounded_line(number, line)}
                    if operation == "search":
                        if needle in line.casefold():  # type: ignore[operator]
                            if len(matches) < MAX_COGNITION_MATCHES:
                                matches.append(item)
                            else:
                                truncated = True
                    else:
                        category = _STRUCTURED_LOG_RECORD.match(line).group(1)  # type: ignore[union-attr]
                        categories[category] = categories.get(category, 0) + 1
                        if len(first) < 8:
                            first.append(item)
                        last.append(item)
                        if len(last) > 16:
                            last.pop(0)
            except (OSError, FileNotFoundError, RunDataUnavailable):
                continue
            if represented:
                run_ids.append(run_id)
        common = {"status": "applied", "operation": operation,
                  "selector": "previous_day", "calendar_date": calendar_date.isoformat(),
                  "timezone": self._timezone_name, "run_ids": run_ids,
                  "safe_line_count": safe_count, "truncated": truncated}
        if operation == "search":
            return {**common, "query": query, "matches": matches}
        by_key = {(item["run_id"], item["line_number"]): item for item in (*first, *last)}
        return {**common, "category_counts": dict(sorted(categories.items())),
                "first_lines": first, "last_lines": last,
                "lines": list(by_key.values()),
                "truncated": truncated or safe_count > len(by_key)}


class RunHistorySetupError(OSError):
    """History initialization failed after a provisional directory claim."""

    def __init__(self, *, cleanup_complete: bool) -> None:
        super().__init__("provisional run setup failed")
        self.cleanup_complete = cleanup_complete


def local_timestamp() -> str:
    """Return the runtime's local ISO-8601 timestamp representation."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass
class RunHistory:
    """The bounded metadata and paths owned by one runtime execution."""

    run_number: int
    directory: Path
    metadata: dict[str, Any]
    clock: Callable[[], str] = local_timestamp
    _authoritative: bool = False

    @property
    def run_id(self) -> str:
        return f"R{self.run_number}"

    @property
    def log_path(self) -> Path:
        return self.directory / "runtime.log"

    @property
    def metadata_path(self) -> Path:
        return self.directory / "run.json"

    def finalize(self, exit_code: int) -> str:
        """Atomically record an authoritative process result."""
        status = (
            "completed" if exit_code == 0 else
            "interrupted" if exit_code == 130 else
            "failed"
        )
        self.metadata.update(
            ended_at=self.clock(), status=status, exit_code=exit_code,
        )
        self._write_metadata()
        return status

    def mark_started(self) -> None:
        """Mark the emitted start record as the authoritative run boundary."""
        self._authoritative = True

    def abort(self) -> bool:
        """Best-effort removal of only this provisional run's owned artifacts."""
        if self._authoritative:
            return False
        complete = True
        for path in (
            self.metadata_path,
            self.directory / ".run.json.tmp",
            self.log_path,
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                complete = False
        try:
            self.directory.rmdir()
        except OSError:
            complete = False
        return complete

    def _write_metadata(self) -> None:
        temporary = self.directory / ".run.json.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(self.metadata, output, indent=2)
                output.write("\n")
                output.flush()
            os.replace(temporary, self.metadata_path)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise


def start_run(
    root: Path,
    *,
    profile: str,
    hardware: str,
    config_source: str | None,
    clock: Callable[[], str] = local_timestamp,
) -> RunHistory:
    """Claim the next R<number> directory and write its initial metadata."""
    root.mkdir(parents=True, exist_ok=True)
    highest = 0
    for child in root.iterdir():
        match = _RUN_DIRECTORY.fullmatch(child.name)
        if match is not None and child.is_dir():
            highest = max(highest, int(match.group(1)))

    candidate = highest + 1
    while True:
        directory = root / f"R{candidate}"
        try:
            directory.mkdir()
            break
        except FileExistsError:
            candidate += 1

    metadata: dict[str, Any] = {
        "schema_version": 1,
        "run_id": f"R{candidate}",
        "run_number": candidate,
        "started_at": clock(),
        "ended_at": None,
        "status": "started",
        "exit_code": None,
        "profile": profile,
        "hardware": hardware,
        "config_source": config_source,
    }
    history = RunHistory(candidate, directory, metadata, clock)
    try:
        history._write_metadata()
    except OSError as error:
        cleanup_complete = history.abort()
        raise RunHistorySetupError(
            cleanup_complete=cleanup_complete
        ) from error
    except BaseException:
        history.abort()
        raise
    return history
