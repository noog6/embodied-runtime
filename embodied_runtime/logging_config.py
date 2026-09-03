"""Small, centralized runtime console logging configuration."""

from datetime import datetime
import logging
import re
import sys
from typing import TextIO

from embodied_runtime.console_style import (
    BRIGHT_RED, RESET, YELLOW, LOG_CATEGORIES, colour_enabled,
)


class LocalISO8601Formatter(logging.Formatter):
    """Prefix records with local wall-clock time including milliseconds and offset."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created).astimezone().isoformat(
            timespec="milliseconds"
        )


class SemanticColourFormatter(LocalISO8601Formatter):
    """Decorate first-party category prefixes without changing log messages."""

    _CATEGORY = re.compile(r"\[([A-Z]+)\]")
    _ERROR_STATE = re.compile(r"(?:^|\s)(?:status=)?(?:rejected|failed|error)(?:\s|$)")
    _WARNING_STATE = re.compile(r"(?:^|\s)(?:status=)?(?:in_flight|warning)(?:\s|$)")

    def __init__(self, fmt: str, *, colour: bool = False) -> None:
        super().__init__(fmt)
        self._colour = colour

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        if not self._colour:
            return rendered
        match = self._CATEGORY.search(rendered)
        if match is None or match.group(1) not in LOG_CATEGORIES:
            return rendered
        ansi = (
            BRIGHT_RED
            if record.levelno >= logging.ERROR or self._ERROR_STATE.search(rendered)
            else YELLOW
            if record.levelno >= logging.WARNING or self._WARNING_STATE.search(rendered)
            else LOG_CATEGORIES[match.group(1)]
        )
        start, end = match.span()
        return f"{rendered[:start]}{ansi}{rendered[start:end]}{RESET}{rendered[end:]}"


def configure_logging(
    *, stream: TextIO = sys.stderr, no_color: bool = False,
) -> None:
    """Configure runtime records for the command-line entry point."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SemanticColourFormatter(
        "%(asctime)s %(message)s",
        colour=colour_enabled(stream, disabled=no_color),
    ))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
