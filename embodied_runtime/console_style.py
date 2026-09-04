"""Small ANSI presentation helpers for the local human-facing console."""

from collections.abc import Mapping
import os
import re
from typing import TextIO


RESET = "\x1b[0m"
BOLD_WHITE = "\x1b[1;97m"
DIM = "\x1b[2m"
DIM_CYAN = "\x1b[2;36m"
CYAN = "\x1b[36m"
BRIGHT_CYAN = "\x1b[96m"
BRIGHT_BLUE = "\x1b[94m"
YELLOW = "\x1b[33m"
DIM_YELLOW = "\x1b[2;33m"
GREEN = "\x1b[32m"
BRIGHT_MAGENTA = "\x1b[95m"
BOLD_BRIGHT_MAGENTA = "\x1b[1;95m"
MAGENTA = "\x1b[35m"
BRIGHT_RED = "\x1b[91m"


def colour_enabled(
    stream: TextIO, *, disabled: bool = False,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether ANSI output is appropriate for this stream."""
    environment = os.environ if environ is None else environ
    if disabled or "NO_COLOR" in environment:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


class ConsoleStyle:
    """Apply a fixed, restrained semantic palette, or return plain text."""

    _VALUES = {
        "true": GREEN, "enabled": GREEN, "running": GREEN, "ready": GREEN,
        "applied": GREEN, "completed": GREEN,
        "false": DIM, "disabled": DIM, "unavailable": DIM, "none": DIM,
        "rejected": BRIGHT_RED, "failed": BRIGHT_RED, "error": BRIGHT_RED,
        "in_flight": YELLOW, "warning": YELLOW,
        "reflex:presence_centering": CYAN, "initiative": CYAN,
        "cognition": CYAN, "console": CYAN,
    }
    _REPORT_VALUE = re.compile(r"^(\s+[^:\n]+:\s+)(\S+)(.*)$")

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def apply(self, text: str, ansi: str) -> str:
        return f"{ansi}{text}{RESET}" if self.enabled else text

    def prompt(self, text: str) -> str:
        return self.apply(text, DIM_CYAN)

    def operator_message(self, name: str, text: str) -> str:
        prefix = self.apply(f"{name}:", BOLD_BRIGHT_MAGENTA)
        message = self.apply(text, BRIGHT_MAGENTA)
        return f"{prefix} {message}"

    def report(self, report: str) -> str:
        if not self.enabled:
            return report
        lines = []
        for line in report.splitlines(keepends=True):
            content = line.rstrip("\r\n")
            ending = line[len(content):]
            match = self._REPORT_VALUE.match(content)
            if match is not None:
                prefix, value, suffix = match.groups()
                ansi = self._VALUES.get(value.lower())
                if ansi is not None:
                    content = f"{prefix}{self.apply(value, ansi)}{suffix}"
            lines.append(content + ending)
        return "".join(lines)


LOG_CATEGORIES = {
    "APP": BOLD_WHITE, "PLATFORM": DIM_CYAN, "HW": CYAN, "CAMERA": CYAN,
    "BODY": BRIGHT_CYAN, "REFLEX": YELLOW, "ATTENTION": BRIGHT_BLUE,
    "INSPECTION": BRIGHT_BLUE,
    "INITIATIVE": BRIGHT_MAGENTA, "INTERACTION": BRIGHT_MAGENTA,
    "CONTINUATION": BRIGHT_BLUE, "OUTCOME": MAGENTA, "GOAL": GREEN,
    "SCENARIO": DIM_YELLOW,
    "PULSE": DIM, "CONSOLE": DIM,
}
