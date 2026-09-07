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

    def test_transport_filter_suppresses_only_namespaced_info(self):
        stream = io.StringIO()
        configure_logging(stream=stream, no_color=True)
        records = (
            ("httpx", logging.INFO, "httpx info"),
            ("httpx._client", logging.INFO, "httpx child info"),
            ("httpcore.some_child", logging.INFO, "httpcore child info"),
            ("openai.some_child", logging.INFO, "openai child info"),
            ("httpx._client", logging.WARNING, "transport warning"),
            ("openai._base_client", logging.ERROR, "client error"),
            ("embodied_runtime.test", logging.INFO, "first-party info"),
            ("other_dependency", logging.INFO, "unrelated info"),
        )
        for name, level, message in records:
            # Set child loggers explicitly to reproduce libraries that override
            # the configured ancestor logger level.
            logging.getLogger(name).setLevel(logging.INFO)
            logging.getLogger(name).log(level, message)
        output = stream.getvalue()
        self.assertNotIn("httpx info", output)
        self.assertNotIn("httpx child info", output)
        self.assertNotIn("httpcore child info", output)
        self.assertNotIn("openai child info", output)
        self.assertIn("transport warning", output)
        self.assertIn("client error", output)
        self.assertIn("first-party info", output)
        self.assertIn("unrelated info", output)

    def test_httpx_info_is_filtered_before_a_library_owned_handler(self):
        """Exercise the handler path that bypasses a filter on the root handler."""
        runtime_stream = io.StringIO()
        library_stream = io.StringIO()
        configure_logging(stream=runtime_stream, no_color=True)
        logger = logging.getLogger("httpx")
        handler = logging.StreamHandler(library_stream)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            logger.info(
                'HTTP Request: POST https://api.openai.com/v1/responses '
                '"HTTP/1.1 200 OK"'
            )
            logger.warning("HTTP transport warning")
        finally:
            logger.removeHandler(handler)

        self.assertNotIn("HTTP Request", library_stream.getvalue())
        self.assertNotIn("HTTP Request", runtime_stream.getvalue())
        self.assertIn("HTTP transport warning", library_stream.getvalue())
        self.assertIn("HTTP transport warning", runtime_stream.getvalue())

    def test_real_httpx_request_summary_uses_protected_logger(self):
        try:
            import httpx
        except ImportError:
            self.skipTest("httpx is available only with the OpenAI extra")

        runtime_stream = io.StringIO()
        library_stream = io.StringIO()
        configure_logging(stream=runtime_stream, no_color=True)
        logger = logging.getLogger("httpx")
        handler = logging.StreamHandler(library_stream)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            with httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text="ok")
            )) as client:
                client.post("https://api.openai.com/v1/responses")
        finally:
            logger.removeHandler(handler)

        self.assertNotIn("HTTP Request", library_stream.getvalue())
        self.assertNotIn("HTTP Request", runtime_stream.getvalue())

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
