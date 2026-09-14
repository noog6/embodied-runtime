"""Small, centralized runtime console logging configuration."""

from datetime import datetime
import logging
import os
import re
import sys
from pathlib import Path
from typing import TextIO

from embodied_runtime.console_style import (
    BRIGHT_RED, DIM, RESET, YELLOW, LOG_CATEGORIES, colour_enabled,
)


_TRANSPORT_LOG_NAMESPACES = ("httpx", "httpcore", "httpx2", "httpcore2", "openai")


class TransportNoiseFilter(logging.Filter):
    """Suppress successful HTTP client chatter while retaining problems."""

    def filter(self, record: logging.LogRecord) -> bool:
        is_transport = any(
            record.name == namespace or record.name.startswith(namespace + ".")
            for namespace in _TRANSPORT_LOG_NAMESPACES
        )
        return not is_transport or record.levelno >= logging.WARNING


class LocalISO8601Formatter(logging.Formatter):
    """Prefix records with local wall-clock time including milliseconds and offset."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created).astimezone().isoformat(
            timespec="milliseconds"
        )


class SemanticColourFormatter(LocalISO8601Formatter):
    """Decorate first-party category prefixes without changing log messages."""

    _CATEGORY = re.compile(r"\[([A-Z]+)\]")
    _TIMESTAMP = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}"
    )
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
        prefix = rendered[:start]
        timestamp = self._TIMESTAMP.match(prefix)
        if timestamp is not None:
            timestamp_end = timestamp.end()
            prefix = (
                f"{DIM}{prefix[:timestamp_end]}{RESET}{prefix[timestamp_end:]}"
            )
        return f"{prefix}{ansi}{rendered[start:end]}{RESET}{rendered[end:]}"


class _OwnedStreamHandler(logging.StreamHandler):
    """A stream handler that closes a stream created solely for its use."""

    def close(self) -> None:
        try:
            self.stream.close()
        finally:
            super().close()


def _stable_stream(stream: TextIO) -> tuple[TextIO, bool]:
    """Duplicate an OS-backed stream so later ``dup2`` calls cannot retarget it."""
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError):
        return stream, False

    duplicate = os.dup(descriptor)
    try:
        stable = os.fdopen(
            duplicate,
            "w",
            encoding=getattr(stream, "encoding", None),
            errors=getattr(stream, "errors", None),
        )
    except BaseException:
        os.close(duplicate)
        raise
    return stable, True


def configure_logging(
    *, stream: TextIO = sys.stderr, no_color: bool = False,
    history_log: Path | None = None,
) -> bool:
    """Configure runtime records for the command-line entry point."""
    handler_stream, owned = _stable_stream(stream)
    handler = (
        _OwnedStreamHandler(handler_stream)
        if owned
        else logging.StreamHandler(handler_stream)
    )
    handlers: list[logging.Handler] = [handler]
    history_available = history_log is None
    try:
        handler.addFilter(TransportNoiseFilter())
        handler.setFormatter(SemanticColourFormatter(
            "%(asctime)s %(message)s",
            colour=colour_enabled(stream, disabled=no_color),
        ))
        if history_log is not None:
            try:
                file_handler = logging.FileHandler(history_log, encoding="utf-8")
            except OSError:
                history_available = False
            else:
                file_handler.addFilter(TransportNoiseFilter())
                file_handler.setFormatter(LocalISO8601Formatter(
                    "%(asctime)s %(message)s"
                ))
                handlers.append(file_handler)
                history_available = True
        logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    except BaseException:
        handler.close()
        raise
    for logger_name in _TRANSPORT_LOG_NAMESPACES:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.WARNING)
        # The OpenAI SDK may configure its dependency loggers after application
        # startup.  A handler owned by ``httpx`` handles records before they
        # reach the filtered root handler, so protect the emitting logger too.
        # In particular, httpx emits its request summary on the exact ``httpx``
        # logger rather than on ``httpx._client``.
        if not any(isinstance(item, TransportNoiseFilter) for item in logger.filters):
            logger.addFilter(TransportNoiseFilter())
    return history_available
