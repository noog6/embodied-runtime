"""SQLite implementation of the durable persistent-memory store."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
import re
import sqlite3
import unicodedata

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

SCHEMA_VERSION = 1
_TOKEN = re.compile(r"^[^\W\d][\w-]*$", re.UNICODE)

_SCHEMA = (
    """CREATE TABLE entities (
        id INTEGER PRIMARY KEY,
        entity_type TEXT NOT NULL,
        canonical_name TEXT NOT NULL,
        canonical_name_normalized TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE entity_aliases (
        id INTEGER PRIMARY KEY,
        entity_id INTEGER NOT NULL REFERENCES entities(id),
        alias TEXT NOT NULL,
        alias_normalized TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(entity_id, alias_normalized)
    )""",
    """CREATE TABLE memories (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        summary TEXT NOT NULL,
        predicate TEXT,
        value_text TEXT,
        source_kind TEXT,
        source_label TEXT,
        confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
        created_at TEXT NOT NULL,
        observed_at TEXT,
        status TEXT NOT NULL
    )""",
    """CREATE TABLE memory_entity_links (
        memory_id INTEGER NOT NULL REFERENCES memories(id),
        entity_id INTEGER NOT NULL REFERENCES entities(id),
        role TEXT NOT NULL,
        PRIMARY KEY(memory_id, entity_id, role)
    )""",
    """CREATE TABLE memory_payloads (
        id INTEGER PRIMARY KEY,
        memory_id INTEGER NOT NULL REFERENCES memories(id),
        payload_kind TEXT NOT NULL,
        media_type TEXT NOT NULL,
        inline_text TEXT,
        object_ref TEXT,
        sha256 TEXT,
        created_at TEXT NOT NULL,
        CHECK((payload_kind = 'text' AND inline_text IS NOT NULL
               AND object_ref IS NULL AND sha256 IS NULL)
              OR (payload_kind = 'object' AND inline_text IS NULL
                  AND object_ref IS NOT NULL AND sha256 IS NOT NULL))
    )""",
    "CREATE INDEX idx_entities_normalized ON entities(canonical_name_normalized)",
    "CREATE INDEX idx_aliases_normalized ON entity_aliases(alias_normalized)",
    "CREATE INDEX idx_memory_links_entity ON memory_entity_links(entity_id, memory_id)",
    "CREATE INDEX idx_memory_payloads_memory ON memory_payloads(memory_id, id)",
    "CREATE INDEX idx_memories_kind_status ON memories(kind, status)",
)


def normalize_entity_name(name: str) -> str:
    """Return the deterministic key used for exact entity-name matching."""
    if not isinstance(name, str):
        raise TypeError("entity name must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", name).split()).casefold()
    if not normalized:
        raise ValueError("entity name must not be empty")
    return normalized


class SQLiteMemoryStore:
    """A serialized, process-local interface to canonical SQLite memory."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout: float = 5.0,
    ) -> None:
        self._clock = clock
        self._connection = sqlite3.connect(path, timeout=timeout, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        try:
            self._initialize_schema()
        except BaseException:
            self._connection.close()
            raise

    def _initialize_schema(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            raise RuntimeError(
                f"unsupported persistent-memory schema version {version}; "
                f"expected {SCHEMA_VERSION}"
            )
        objects = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if objects is not None:
            raise RuntimeError("unversioned non-empty persistent-memory database")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA:
                self._connection.execute(statement)
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def create_entity(self, entity_type: str, canonical_name: str) -> EntityRecord:
        entity_type = _validated_token(entity_type, "entity type")
        display_name = _display_name(canonical_name)
        created_at = self._now()
        cursor = self._connection.execute(
            """INSERT INTO entities
               (entity_type, canonical_name, canonical_name_normalized, created_at)
               VALUES (?, ?, ?, ?)""",
            (entity_type, display_name, normalize_entity_name(display_name), _format_time(created_at)),
        )
        return EntityRecord(cursor.lastrowid, entity_type, display_name, created_at)

    def get_entity(self, entity_id: int) -> EntityRecord | None:
        _validated_id(entity_id, "entity")
        row = self._connection.execute(
            "SELECT id, entity_type, canonical_name, created_at FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        return _entity(row) if row is not None else None

    def add_entity_alias(self, entity_id: int, alias: str) -> EntityAlias:
        _validated_id(entity_id, "entity")
        display_alias = _display_name(alias)
        created_at = self._now()
        try:
            cursor = self._connection.execute(
                """INSERT INTO entity_aliases
                   (entity_id, alias, alias_normalized, created_at) VALUES (?, ?, ?, ?)""",
                (entity_id, display_alias, normalize_entity_name(display_alias),
                 _format_time(created_at)),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError("alias could not be added to the entity") from error
        return EntityAlias(cursor.lastrowid, entity_id, display_alias, created_at)

    def find_entities_exact(self, name: str) -> tuple[EntityRecord, ...]:
        normalized = normalize_entity_name(name)
        rows = self._connection.execute(
            """SELECT DISTINCT e.id, e.entity_type, e.canonical_name, e.created_at
               FROM entities AS e
               LEFT JOIN entity_aliases AS a ON a.entity_id = e.id
               WHERE e.canonical_name_normalized = ? OR a.alias_normalized = ?
               ORDER BY e.id""",
            (normalized, normalized),
        ).fetchall()
        return tuple(_entity(row) for row in rows)

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
    ) -> StoredMemory:
        kind = _validated_token(kind, "memory kind")
        summary = _nonempty(summary, "summary")
        predicate = _optional_token(predicate, "predicate")
        value_text = _optional_nonempty(value_text, "value text")
        source_kind = _optional_token(source_kind, "source kind")
        source_label = _optional_nonempty(source_label, "source label")
        if confidence is not None and (
            isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not 0.0 <= confidence <= 1.0
        ):
            raise ValueError("confidence must be between 0.0 and 1.0")
        if status != "active":
            raise ValueError("only active memory status is supported")
        if observed_at is not None:
            observed_at = _utc_time(observed_at)
        links = tuple(links)
        payloads = tuple(payloads)
        if not payloads:
            raise ValueError("a memory requires at least one payload")
        for link in links:
            _validated_id(link.entity_id, "entity")
            _validated_token(link.role, "role")
        for payload in payloads:
            _validate_payload(payload)

        created_at = self._now()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(
                """INSERT INTO memories
                   (kind, summary, predicate, value_text, source_kind, source_label,
                    confidence, created_at, observed_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (kind, summary, predicate, value_text, source_kind, source_label,
                 confidence, _format_time(created_at),
                 _format_time(observed_at) if observed_at else None, status),
            )
            memory_id = cursor.lastrowid
            for link in links:
                self._connection.execute(
                    "INSERT INTO memory_entity_links (memory_id, entity_id, role) VALUES (?, ?, ?)",
                    (memory_id, link.entity_id, link.role),
                )
            for payload in payloads:
                self._connection.execute(
                    """INSERT INTO memory_payloads
                       (memory_id, payload_kind, media_type, inline_text, object_ref,
                        sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (memory_id, payload.payload_kind, payload.media_type,
                     payload.inline_text, payload.object_ref, payload.sha256,
                     _format_time(created_at)),
                )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            self._connection.rollback()
            raise ValueError("memory links or payloads violate store constraints") from error
        except BaseException:
            self._connection.rollback()
            raise
        stored = self.get_memory(memory_id)
        assert stored is not None
        return stored

    def get_memory(self, memory_id: int) -> StoredMemory | None:
        _validated_id(memory_id, "memory")
        row = self._connection.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return None
        return self._stored_memory(row)

    def list_memories_for_entity(self, entity_id: int) -> tuple[StoredMemory, ...]:
        _validated_id(entity_id, "entity")
        rows = self._connection.execute(
            """SELECT m.* FROM memories AS m
               WHERE EXISTS (
                   SELECT 1 FROM memory_entity_links AS l
                   WHERE l.memory_id = m.id AND l.entity_id = ?
               )
               ORDER BY m.id""",
            (entity_id,),
        ).fetchall()
        return tuple(self._stored_memory(row) for row in rows)

    def _stored_memory(self, row: sqlite3.Row) -> StoredMemory:
        memory_id = row["id"]
        link_rows = self._connection.execute(
            """SELECT memory_id, entity_id, role FROM memory_entity_links
               WHERE memory_id = ? ORDER BY entity_id, role""",
            (memory_id,),
        ).fetchall()
        payload_rows = self._connection.execute(
            "SELECT * FROM memory_payloads WHERE memory_id = ? ORDER BY id",
            (memory_id,),
        ).fetchall()
        return StoredMemory(
            MemoryRecord(
                memory_id, row["kind"], row["summary"], row["predicate"],
                row["value_text"], row["source_kind"], row["source_label"],
                row["confidence"], _parse_time(row["created_at"]),
                _parse_time(row["observed_at"]) if row["observed_at"] else None,
                row["status"],
            ),
            tuple(MemoryEntityLink(item["memory_id"], item["entity_id"], item["role"])
                  for item in link_rows),
            tuple(MemoryPayload(
                item["id"], item["memory_id"], item["payload_kind"],
                item["media_type"], item["inline_text"], item["object_ref"],
                item["sha256"], _parse_time(item["created_at"]),
            ) for item in payload_rows),
        )

    def _now(self) -> datetime:
        return _utc_time(self._clock())

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SQLiteMemoryStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _entity(row: sqlite3.Row) -> EntityRecord:
    return EntityRecord(row["id"], row["entity_type"], row["canonical_name"],
                        _parse_time(row["created_at"]))


def _display_name(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("entity name must be a string")
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    if not value:
        raise ValueError("entity name must not be empty")
    return value


def _nonempty(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _optional_nonempty(value: str | None, label: str) -> str | None:
    return None if value is None else _nonempty(value, label)


def _validated_token(value: str, label: str) -> str:
    value = _nonempty(value, label)
    if _TOKEN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a simple identifier")
    return value


def _optional_token(value: str | None, label: str) -> str | None:
    return None if value is None else _validated_token(value, label)


def _validated_id(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} ID must be a positive integer")
    return value


def _validate_payload(payload: NewMemoryPayload) -> None:
    if not isinstance(payload, NewMemoryPayload):
        raise TypeError("payloads must be NewMemoryPayload records")
    _validated_token(payload.payload_kind, "payload kind")
    _nonempty(payload.media_type, "media type")
    if payload.payload_kind != "text":
        raise ValueError("Phase 18.1 supports creating text payloads only")
    if payload.inline_text is None or not payload.inline_text.strip():
        raise ValueError("text payloads require inline text")
    if payload.object_ref is not None or payload.sha256 is not None:
        raise ValueError("text payloads cannot contain object references")


def _utc_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("persistent timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
