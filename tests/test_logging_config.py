import logging
import io
import re
import unittest
from unittest.mock import patch

from embodied_runtime.logging_config import (
    LocalISO8601Formatter, SemanticColourFormatter, configure_logging,
)
from embodied_runtime.console_style import BRIGHT_RED, YELLOW


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

    def test_failure_and_warning_states_override_category_colour(self):
        formatter = SemanticColourFormatter("%(message)s", colour=True)
        for state, ansi in (("failed", BRIGHT_RED), ("in_flight", YELLOW)):
            message = f"[INITIATIVE] status={state}"
            record = logging.LogRecord(
                "test", logging.INFO, __file__, 1, message, (), None
            )
            self.assertIn(ansi, formatter.format(record))

    def test_non_tty_logging_output_is_plain_and_redirection_safe(self):
        stream = io.StringIO()
        configure_logging(stream=stream)
        logging.getLogger("embodied_runtime.test").info("[BODY] status=ready")
        self.assertNotIn("\x1b[", stream.getvalue())
        self.assertIn("[BODY] status=ready", stream.getvalue())

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
