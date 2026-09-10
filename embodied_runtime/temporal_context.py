"""Provider-neutral current wall-clock grounding for cognition."""

from dataclasses import dataclass
from datetime import datetime
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
