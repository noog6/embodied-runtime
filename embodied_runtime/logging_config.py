"""Small, centralized runtime console logging configuration."""

from datetime import datetime
import logging


class LocalISO8601Formatter(logging.Formatter):
    """Prefix records with local wall-clock time including milliseconds and offset."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created).astimezone().isoformat(
            timespec="milliseconds"
        )


def configure_logging() -> None:
    """Configure runtime records for the command-line entry point."""
    handler = logging.StreamHandler()
    handler.setFormatter(LocalISO8601Formatter("%(asctime)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
