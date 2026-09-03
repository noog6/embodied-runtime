import logging
import io
import re
import unittest
from unittest.mock import patch

from embodied_runtime.logging_config import (
    LocalISO8601Formatter, SemanticColourFormatter, configure_logging,
)
from embodied_runtime.console_style import BRIGHT_BLUE, BRIGHT_RED, DIM, RESET, YELLOW


ANSI = re.compile(r"\x1b\[[0-9;]*m")


class TtyStream(io.StringIO):
    def isatty(self):
        return True


class LoggingFormatterTests(unittest.TestCase):
    def test_local_iso_timestamp_has_milliseconds_and_offset(self):
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "[APP] ready", (), None
        )
        record.created = 1_788_120_342.137
        rendered = LocalISO8601Formatter("%(asctime)s %(message)s").format(record)
        self.assertRegex(
            rendered,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} \[APP\] ready$",
        )
        self.assertEqual(rendered.count("[APP]"), 1)

    def test_first_party_categories_are_decorated_without_semantic_changes(self):
        formatter = SemanticColourFormatter("%(message)s", colour=True)
        for category in ("BODY", "ATTENTION", "INITIATIVE", "INTERACTION", "OUTCOME"):
            message = f"[{category}] event=test status=applied"
            record = logging.LogRecord("test", logging.INFO, __file__, 1, message, (), None)
            rendered = formatter.format(record)
            self.assertIn("\x1b[", rendered)
            self.assertEqual(ANSI.sub("", rendered), message)

    def test_timestamp_is_dim_and_category_keeps_semantic_colour(self):
        plain = "2026-09-03T18:28:27.968-04:00 [ATTENTION] decision=wake"
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "[ATTENTION] decision=wake", (), None
        )
        record.created = 1_788_120_342.137
        formatter = SemanticColourFormatter("%(asctime)s %(message)s", colour=True)
        with patch.object(formatter, "formatTime", return_value=plain.split(" ", 1)[0]):
            rendered = formatter.format(record)
        self.assertIn(f"{DIM}{plain.split(' ', 1)[0]}{RESET}", rendered)
        self.assertIn(f"{BRIGHT_BLUE}[ATTENTION]{RESET}", rendered)
        self.assertEqual(ANSI.sub("", rendered), plain)

    def test_failure_and_warning_states_override_category_colour(self):
        formatter = SemanticColourFormatter("%(message)s", colour=True)
        for state, ansi in (("failed", BRIGHT_RED), ("in_flight", YELLOW)):
            message = f"[INITIATIVE] status={state}"
            record = logging.LogRecord(
                "test", logging.INFO, __file__, 1, message, (), None
            )
            self.assertIn(ansi, formatter.format(record))

    def test_failure_override_and_dim_timestamp_are_combined(self):
        timestamp = "2026-09-03T18:28:27.968-04:00"
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "[INITIATIVE] status=failed", (), None
        )
        formatter = SemanticColourFormatter("%(asctime)s %(message)s", colour=True)
        with patch.object(formatter, "formatTime", return_value=timestamp):
            rendered = formatter.format(record)
        self.assertIn(f"{DIM}{timestamp}{RESET}", rendered)
        self.assertIn(f"{BRIGHT_RED}[INITIATIVE]{RESET}", rendered)

    def test_non_tty_logging_output_is_plain_and_redirection_safe(self):
        stream = io.StringIO()
        configure_logging(stream=stream)
        logging.getLogger("embodied_runtime.test").info("[BODY] status=ready")
        self.assertNotIn("\x1b[", stream.getvalue())
        self.assertIn("[BODY] status=ready", stream.getvalue())

    def test_plain_formatter_preserves_timestamp_and_category_exactly(self):
        plain = "2026-09-03T18:28:27.968-04:00 [ATTENTION] decision=wake"
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "[ATTENTION] decision=wake", (), None
        )
        formatter = SemanticColourFormatter("%(asctime)s %(message)s", colour=False)
        with patch.object(formatter, "formatTime", return_value=plain.split(" ", 1)[0]):
            self.assertEqual(formatter.format(record), plain)

    def test_third_party_text_is_not_rewritten(self):
        message = "HTTP Request: POST https://example.invalid status=failed"
        record = logging.LogRecord("httpx", logging.INFO, __file__, 1, message, (), None)
        formatter = SemanticColourFormatter("%(message)s", colour=True)
        self.assertEqual(formatter.format(record), message)

    def test_tty_logging_colour_obeys_no_color_override(self):
        coloured = TtyStream()
        with patch.dict("os.environ", {}, clear=True):
            configure_logging(stream=coloured)
            logging.getLogger("embodied_runtime.test").info("[ATTENTION] decision=wake")
        self.assertIn("\x1b[", coloured.getvalue())
        plain = TtyStream()
        configure_logging(stream=plain, no_color=True)
        logging.getLogger("embodied_runtime.test").info("[ATTENTION] decision=wake")
        self.assertNotIn("\x1b[", plain.getvalue())
