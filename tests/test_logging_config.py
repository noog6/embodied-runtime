import logging
import unittest

from embodied_runtime.logging_config import LocalISO8601Formatter


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
