"""Persistent-memory store contract."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from .model import (
    EntityAlias,
    EntityRecord,
    NewMemoryLink,
    NewMemoryPayload,
    StoredMemory,
)


class PersistentMemoryStore(Protocol):
    def create_entity(self, entity_type: str, canonical_name: str) -> EntityRecord: ...

    def get_entity(self, entity_id: int) -> EntityRecord | None: ...

    def add_entity_alias(self, entity_id: int, alias: str) -> EntityAlias: ...

    def find_entities_exact(self, name: str) -> tuple[EntityRecord, ...]: ...

    def create_memory(
        self,
        kind: str,
        summary: str,
        *,
        payloads: Sequence[NewMemoryPayload],
        links: Sequence[NewMemoryLink] = (),
        predicate: str | None = None,
        value_text: str | None = None,
        source_kind: str | None = None,
        source_label: str | None = None,
        confidence: float | None = None,
        observed_at: datetime | None = None,
        status: str = "active",
    ) -> StoredMemory: ...

    def get_memory(self, memory_id: int) -> StoredMemory | None: ...

    def list_memories_for_entity(self, entity_id: int) -> tuple[StoredMemory, ...]: ...

    def close(self) -> None: ...
