from datetime import UTC, datetime
from dataclasses import FrozenInstanceError
import unittest
from zoneinfo import ZoneInfo

from embodied_runtime.temporal_context import (
    TemporalContext, TemporalSituation, format_duration,
)


class LocaleSensitiveDatetime(datetime):
    def strftime(self, format):
        if format == "%A":
            return "locale-dependent-weekday"
        return super().strftime(format)


class TemporalContextTests(unittest.TestCase):
    def context(self, year=2026, month=9, day=10, hour=18, minute=7, second=42):
        local = datetime(year, month, day, hour, minute, second,
                         tzinfo=ZoneInfo("America/Toronto"))
        return TemporalContext(local, "America/Toronto")

    def test_shape_is_immutable_and_rendering_is_exact(self):
        context = self.context()
        with self.assertRaises(FrozenInstanceError):
            context.timezone_name = "UTC"
        self.assertEqual(context.render(), """Temporal context
The following time information is supplied by the robot runtime and is
authoritative for the moment this cognition grounding was constructed.

  local_datetime: 2026-09-10T18:07:42-04:00
  date: 2026-09-10
  weekday: Thursday
  local_time: 18:07:42
  timezone: America/Toronto
  utc_offset: -04:00
  day_period: evening""")

    def test_aware_utc_instant_is_converted_to_configured_zone(self):
        context = TemporalContext.from_instant(
            datetime(2026, 9, 10, 22, 7, 42, tzinfo=UTC), "America/Toronto"
        )
        self.assertEqual(context.local_datetime.isoformat(),
                         "2026-09-10T18:07:42-04:00")

    def test_weekday_rendering_does_not_use_locale_sensitive_strftime(self):
        local = LocaleSensitiveDatetime(
            2026, 9, 10, 18, 7, 42, tzinfo=ZoneInfo("America/Toronto")
        )
        rendered = TemporalContext(local, "America/Toronto").render()
        self.assertIn("weekday: Thursday", rendered)
        self.assertNotIn("locale-dependent-weekday", rendered)

    def test_zoneinfo_supplies_winter_and_summer_offsets(self):
        winter = TemporalContext.from_instant(
            datetime(2026, 1, 10, 17, tzinfo=UTC), "America/Toronto")
        summer = TemporalContext.from_instant(
            datetime(2026, 7, 10, 16, tzinfo=UTC), "America/Toronto")
        self.assertIn("utc_offset: -05:00", winter.render())
        self.assertIn("utc_offset: -04:00", summer.render())

    def test_day_period_boundaries(self):
        cases = ((4, 59, "night"), (5, 0, "morning"),
                 (11, 59, "morning"), (12, 0, "afternoon"),
                 (16, 59, "afternoon"), (17, 0, "evening"),
                 (21, 59, "evening"), (22, 0, "night"))
        for hour, minute, expected in cases:
            with self.subTest(time=f"{hour:02}:{minute:02}"):
                self.assertEqual(self.context(hour=hour, minute=minute).day_period,
                                 expected)

    def test_naive_datetime_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "offset-aware"):
            TemporalContext(datetime(2026, 1, 1), "UTC")


class TemporalSituationTests(unittest.TestCase):
    def test_empty_rendering_is_explicit_and_immutable(self):
        situation = TemporalSituation(None, None, "none", None, None, None, None, None)
        rendered = situation.render()
        self.assertEqual(rendered.count("  state: none"), 4)
        self.assertIn("No temporal follow-up is currently scheduled.", rendered)
        self.assertIn("No active goal is currently in progress.", rendered)
        self.assertIn("Only Follow-up state pending or due_pending", rendered)
        with self.assertRaises(FrozenInstanceError):
            situation.followup_state = "pending"

    def test_fully_populated_rendering_and_quoted_purpose(self):
        situation = TemporalSituation(4, 742, "pending", 182,
                                      'check "battery" voltage', 38, 12, 38)
        rendered = situation.render()
        self.assertIn("  id: G4\n  age_s: 742", rendered)
        self.assertIn('purpose: "check \\"battery\\" voltage"', rendered)
        self.assertIn("One follow-up is pending in about 3 minutes.", rendered)
        self.assertNotIn("No temporal follow-up is currently scheduled.", rendered)
        self.assertIn("  id: E12\n  age_s: 38", rendered)

    def test_active_goal_does_not_imply_a_followup(self):
        rendered = TemporalSituation(
            1, 30, "none", None, None, None, None, None
        ).render()
        self.assertIn("Active goal timing\n  state: active\n  id: G1", rendered)
        self.assertIn("Follow-up\n  state: none", rendered)
        self.assertIn("No temporal follow-up is currently scheduled.", rendered)

    def test_due_pending_is_due_now(self):
        rendered = TemporalSituation(
            1, 30, "due_pending", 0, "check battery", None, None, None
        ).render()
        self.assertIn("Follow-up\n  state: due_pending", rendered)
        self.assertIn("One follow-up is due now.", rendered)
        self.assertNotIn("No temporal follow-up is currently scheduled.", rendered)

    def test_duration_buckets_are_deterministic_and_clamped(self):
        cases = {
            -1: "a few seconds", 9: "a few seconds",
            10: "less than a minute", 59: "less than a minute",
            60: "about 1 minute", 119: "about 1 minute",
            120: "about 2 minutes", 3599: "about 59 minutes",
            3600: "about 1 hour", 7199: "about 1 hour",
            7200: "about 2 hours", 86399: "about 23 hours",
            86400: "about 1 day", 172800: "about 2 days",
        }
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration(seconds), expected)
