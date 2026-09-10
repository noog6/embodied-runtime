"""Provider-neutral wall-clock and monotonic grounding for cognition."""

from dataclasses import dataclass
from datetime import datetime
import json
from zoneinfo import ZoneInfo

WEEKDAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)


@dataclass(frozen=True, slots=True)
class TemporalContext:
    """An authoritative local instant projected into a configured timezone."""

    local_datetime: datetime
    timezone_name: str

    def __post_init__(self) -> None:
        if self.local_datetime.tzinfo is None or self.local_datetime.utcoffset() is None:
            raise ValueError("TemporalContext.local_datetime must be offset-aware")

    @classmethod
    def from_instant(cls, instant: datetime, timezone_name: str) -> "TemporalContext":
        """Convert an aware instant into the configured local presentation zone."""
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("wall clock must return an offset-aware datetime")
        return cls(instant.astimezone(ZoneInfo(timezone_name)), timezone_name)

    @property
    def day_period(self) -> str:
        hour = self.local_datetime.hour
        if 5 <= hour < 12:
            return "morning"
        if 12 <= hour < 17:
            return "afternoon"
        if 17 <= hour < 22:
            return "evening"
        return "night"

    def render(self) -> str:
        offset = self.local_datetime.strftime("%z")
        formatted_offset = f"{offset[:3]}:{offset[3:]}"
        return "\n".join((
            "Temporal context",
            "The following time information is supplied by the robot runtime and is",
            "authoritative for the moment this cognition grounding was constructed.",
            "",
            f"  local_datetime: {self.local_datetime.isoformat(timespec='seconds')}",
            f"  date: {self.local_datetime.date().isoformat()}",
            f"  weekday: {WEEKDAY_NAMES[self.local_datetime.weekday()]}",
            f"  local_time: {self.local_datetime.time().isoformat(timespec='seconds')}",
            f"  timezone: {self.timezone_name}",
            f"  utc_offset: {formatted_offset}",
            f"  day_period: {self.day_period}",
        ))


@dataclass(frozen=True, slots=True)
class TemporalSituation:
    """Immediate timing relationships reconstructed from runtime-owned state."""

    active_goal_id: int | None
    active_goal_age_seconds: int | None
    followup_state: str
    followup_remaining_seconds: int | None
    followup_purpose: str | None
    last_operator_turn_age_seconds: int | None
    last_completed_episode_id: int | None
    last_completed_episode_age_seconds: int | None

    def render(self) -> str:
        lines = [
            "Temporal situation",
            "The following temporal relationships are reconstructed from runtime-owned",
            "monotonic state. They describe recency and pending commitments, not physical",
            "reality or conversation meaning.",
            "",
            "Active goal timing",
        ]
        if self.active_goal_id is None:
            lines.append("  state: none")
        else:
            lines.extend(("  state: active", f"  id: G{self.active_goal_id}",
                          f"  age_s: {_age(self.active_goal_age_seconds)}"))
        lines.extend(("", "Follow-up", f"  state: {self.followup_state}"))
        if self.followup_state != "none":
            lines.extend((
                f"  remaining_s: {_age(self.followup_remaining_seconds)}",
                f"  purpose: {json.dumps(self.followup_purpose, ensure_ascii=False)}",
            ))
        lines.extend(("", "Previous operator turn"))
        if self.last_operator_turn_age_seconds is None:
            lines.append("  state: none")
        else:
            lines.extend(("  state: available",
                          f"  age_s: {_age(self.last_operator_turn_age_seconds)}"))
        lines.extend(("", "Previous completed episode"))
        if self.last_completed_episode_id is None:
            lines.append("  state: none")
        else:
            lines.extend((
                "  state: available", f"  id: E{self.last_completed_episode_id}",
                f"  age_s: {_age(self.last_completed_episode_age_seconds)}",
            ))
        lines.extend(("", "Situation summary"))
        lines.extend(f"  {sentence}" for sentence in self.summary())
        return "\n".join(lines)

    def summary(self) -> tuple[str, ...]:
        lines: list[str] = []
        if self.active_goal_id is not None:
            lines.append("An active goal has been in progress for "
                         f"{format_duration(_age(self.active_goal_age_seconds))}.")
        if self.followup_state != "none":
            if self.followup_state == "due_pending":
                lines.append("One follow-up is due now.")
            else:
                lines.append("One follow-up is pending in "
                             f"{format_duration(_age(self.followup_remaining_seconds))}.")
        if self.active_goal_id is None and self.followup_state == "none":
            lines.append("No active goal or temporal follow-up is currently pending.")
        if self.last_operator_turn_age_seconds is not None:
            lines.append("The previous operator turn completed "
                         f"{format_duration(_age(self.last_operator_turn_age_seconds))} ago.")
        if self.last_completed_episode_id is not None:
            lines.append("The previous deliberative episode completed "
                         f"{format_duration(_age(self.last_completed_episode_age_seconds))} ago.")
        return tuple(lines)


def _age(value: int | None) -> int:
    return max(0, value or 0)


def format_duration(seconds: int) -> str:
    """Render a nonnegative duration using deterministic, locale-free buckets."""
    seconds = max(0, seconds)
    if seconds < 10:
        return "a few seconds"
    if seconds < 60:
        return "less than a minute"
    if seconds < 120:
        return "about 1 minute"
    if seconds < 3600:
        return f"about {seconds // 60} minutes"
    if seconds < 7200:
        return "about 1 hour"
    if seconds < 86400:
        return f"about {seconds // 3600} hours"
    days = seconds // 86400
    return "about 1 day" if days == 1 else f"about {days} days"
