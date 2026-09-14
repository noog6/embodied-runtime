"""Small, local, append-only records for runtime executions."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
from typing import Any


DEFAULT_HISTORY_ROOT = Path("data/runs")
_RUN_DIRECTORY = re.compile(r"R([1-9][0-9]*)\Z")


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
