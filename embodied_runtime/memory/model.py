"""Immutable public records for durable persistent memory."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class EntityRecord:
    id: int
    entity_type: str
    canonical_name: str
    created_at: datetime

    @property
    def identity(self) -> str:
        return f"ENT{self.id}"


@dataclass(frozen=True, slots=True)
class EntityAlias:
    id: int
    entity_id: int
    alias: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: int
    kind: str
    summary: str
    predicate: str | None
    value_text: str | None
    source_kind: str | None
    source_label: str | None
    confidence: float | None
    created_at: datetime
    observed_at: datetime | None
    status: str

    @property
    def identity(self) -> str:
        return f"MEM{self.id}"


@dataclass(frozen=True, slots=True)
class MemoryEntityLink:
    memory_id: int
    entity_id: int
    role: str


@dataclass(frozen=True, slots=True)
class MemoryPayload:
    id: int
    memory_id: int
    payload_kind: str
    media_type: str
    inline_text: str | None
    object_ref: str | None
    sha256: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NewMemoryLink:
    """An entity link requested as part of an atomic memory write."""

    entity_id: int
    role: str


@dataclass(frozen=True, slots=True)
class NewMemoryPayload:
    """A payload requested as part of an atomic memory write."""

    payload_kind: str
    media_type: str
    inline_text: str | None = None
    object_ref: str | None = None
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class StoredMemory:
    """A memory record together with its durable links and payloads."""

    record: MemoryRecord
    links: tuple[MemoryEntityLink, ...]
    payloads: tuple[MemoryPayload, ...]
