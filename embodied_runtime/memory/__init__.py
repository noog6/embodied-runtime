"""Durable, local persistent-memory substrate."""

from .model import (
    EntityAlias,
    EntityRecord,
    MemoryEntityLink,
    MemoryPayload,
    MemoryRecord,
    NewMemoryLink,
    NewMemoryPayload,
    StoredMemory,
)
from .sqlite_store import SQLiteMemoryStore
from .store import PersistentMemoryStore
from .recall import (
    MAX_RECALL_ENTITIES, MAX_RECALL_MEMORIES_PER_ENTITY, MAX_RECALL_OUTPUT_CHARS,
    MAX_RECALL_QUERY_CHARS, MemoryRecallProjector, MemoryRecallResult,
    RecalledEntity, RecalledLink, RecalledMemory,
)

__all__ = [
    "EntityAlias",
    "EntityRecord",
    "MemoryEntityLink",
    "MemoryPayload",
    "MemoryRecord",
    "NewMemoryLink",
    "NewMemoryPayload",
    "PersistentMemoryStore",
    "MemoryRecallProjector",
    "MemoryRecallResult",
    "RecalledEntity",
    "RecalledLink",
    "RecalledMemory",
    "MAX_RECALL_ENTITIES",
    "MAX_RECALL_MEMORIES_PER_ENTITY",
    "MAX_RECALL_OUTPUT_CHARS",
    "MAX_RECALL_QUERY_CHARS",
    "SQLiteMemoryStore",
    "StoredMemory",
]
