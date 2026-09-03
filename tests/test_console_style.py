import re
import unittest

from embodied_runtime.console_style import (
    BOLD_BRIGHT_MAGENTA, BRIGHT_RED, DIM, DIM_CYAN, GREEN, RESET, YELLOW,
    ConsoleStyle, colour_enabled,
)


ANSI = re.compile(r"\x1b\[[0-9;]*m")


class FakeStream:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


class ConsoleStyleTests(unittest.TestCase):
    def test_tty_no_color_and_non_tty_detection(self):
        self.assertTrue(colour_enabled(FakeStream(True), environ={}))
        self.assertFalse(colour_enabled(FakeStream(False), environ={}))
        self.assertFalse(colour_enabled(FakeStream(True), environ={"NO_COLOR": ""}))
        self.assertFalse(colour_enabled(FakeStream(True), disabled=True, environ={}))

    def test_operator_message_is_neon_but_semantically_unchanged(self):
        rendered = ConsoleStyle(True).operator_message("Mira", "Hello.")
        self.assertIn(f"{BOLD_BRIGHT_MAGENTA}Mira:{RESET}", rendered)
        self.assertEqual(ANSI.sub("", rendered), "Mira: Hello.")
        self.assertEqual(
            ConsoleStyle(False).operator_message("Mira", "Hello."), "Mira: Hello."
        )

    def test_prompt_is_subdued_cyan_or_exactly_plain(self):
        self.assertEqual(ConsoleStyle(True).prompt("mira> "), f"{DIM_CYAN}mira> {RESET}")
        self.assertEqual(ConsoleStyle(False).prompt("mira> "), "mira> ")

    def test_known_diagnostic_values_receive_semantic_colours(self):
        report = (
            "Attention\n  one: applied\n  two: completed\n  three: rejected\n"
            "  four: failed\n  five: in_flight\n  six: none\n  seven: unavailable"
        )
        rendered = ConsoleStyle(True).report(report)
        for value in ("applied", "completed"):
            self.assertIn(f"{GREEN}{value}{RESET}", rendered)
        for value in ("rejected", "failed"):
            self.assertIn(f"{BRIGHT_RED}{value}{RESET}", rendered)
        self.assertIn(f"{YELLOW}in_flight{RESET}", rendered)
        for value in ("none", "unavailable"):
            self.assertIn(f"{DIM}{value}{RESET}", rendered)
        self.assertEqual(ANSI.sub("", rendered), report)

    def test_plain_report_is_exact(self):
        report = "Body\n  state:         unavailable"
        self.assertEqual(ConsoleStyle(False).report(report), report)
