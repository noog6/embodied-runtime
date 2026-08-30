"""Base type for transient runtime events."""

from dataclasses import dataclass, field
from time import monotonic_ns


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """A discrete semantic fact observed within this runtime process."""

    source: str
    timestamp_ns: int = field(default_factory=monotonic_ns)
