"""SQLite append-only conversation-history implementation."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

from embodied_runtime.interaction import InteractionChannel
from .model import (
    MAX_ASSISTANT_TEXT_CHARS, MAX_CROSS_CHANNEL_RECORDS, MAX_OPERATOR_TEXT_CHARS,
    MAX_SAME_CHANNEL_RECORDS, ConversationTurnRecord, NewConversationTurn,
    bounded_text,
)

SCHEMA_VERSION = 1
_CHANNELS = tuple(channel.value for channel in InteractionChannel)


class SQLiteConversationHistoryStore:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        del clock  # Reserved for consistency with other stores; records carry completion time.
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        tables = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        if version not in (0, SCHEMA_VERSION):
            raise ValueError(f"unsupported conversation schema version {version}")
        if version == 0 and tables:
            raise ValueError("unversioned non-empty conversation database")
        allowed = ", ".join(f"'{value}'" for value in _CHANNELS)
        self._connection.execute(f"""CREATE TABLE IF NOT EXISTS conversation_turns (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            channel TEXT NOT NULL CHECK(channel IN ({allowed})),
            operator_text TEXT NOT NULL,
            assistant_text TEXT NOT NULL
        )""")
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS conversation_turns_recent "
            "ON conversation_turns (completed_at DESC, id DESC)"
        )
        if version == 0:
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._connection.commit()

    def append(self, turn: NewConversationTurn) -> ConversationTurnRecord:
        if not isinstance(turn.channel, InteractionChannel):
            raise ValueError("invalid conversation channel")
        if not isinstance(turn.session_id, str) or not turn.session_id:
            raise ValueError("session_id must be a non-empty string")
        completed_at = _aware_utc(turn.completed_at)
        operator = bounded_text(turn.operator_text, MAX_OPERATOR_TEXT_CHARS)
        assistant = bounded_text(turn.assistant_text, MAX_ASSISTANT_TEXT_CHARS)
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO conversation_turns "
                "(session_id, completed_at, channel, operator_text, assistant_text) "
                "VALUES (?, ?, ?, ?, ?)",
                (turn.session_id, completed_at.isoformat(), turn.channel.value,
                 operator, assistant),
            )
        return ConversationTurnRecord(
            int(cursor.lastrowid), turn.session_id, completed_at, turn.channel,
            operator, assistant,
        )

    def select_prior_session(
        self, current_session_id: str, current_channel: InteractionChannel,
        *, same_channel_limit: int = MAX_SAME_CHANNEL_RECORDS,
        cross_channel_limit: int = MAX_CROSS_CHANNEL_RECORDS,
    ) -> tuple[ConversationTurnRecord, ...]:
        if not isinstance(current_channel, InteractionChannel):
            raise ValueError("invalid conversation channel")
        if min(same_channel_limit, cross_channel_limit) < 0:
            raise ValueError("selection limits must be non-negative")
        same = self._select(current_session_id, "channel = ?", (current_channel.value,),
                            same_channel_limit)
        cross = self._select(current_session_id, "channel != ?", (current_channel.value,),
                             cross_channel_limit)
        return tuple(sorted((*same, *cross), key=lambda item: (item.completed_at, item.id)))

    def _select(self, session_id: str, predicate: str, values: tuple[str, ...],
                limit: int) -> tuple[ConversationTurnRecord, ...]:
        if limit == 0:
            return ()
        rows = self._connection.execute(
            f"SELECT id, session_id, completed_at, channel, operator_text, assistant_text "
            f"FROM conversation_turns WHERE session_id != ? AND {predicate} "
            "ORDER BY completed_at DESC, id DESC LIMIT ?",
            (session_id, *values, limit),
        ).fetchall()
        return tuple(_record(row) for row in rows)

    def close(self) -> None:
        self._connection.close()


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("completed_at must be an offset-aware datetime")
    return value.astimezone(UTC)


def _record(row: sqlite3.Row) -> ConversationTurnRecord:
    try:
        channel = InteractionChannel(row["channel"])
        completed_at = datetime.fromisoformat(row["completed_at"])
        completed_at = _aware_utc(completed_at)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid stored conversation record") from error
    return ConversationTurnRecord(
        row["id"], row["session_id"], completed_at, channel,
        row["operator_text"], row["assistant_text"],
    )
