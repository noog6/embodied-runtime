"""Durable, bounded dialogue history for cross-session continuity."""

from .model import (
    MAX_ASSISTANT_TEXT_CHARS, MAX_CROSS_CHANNEL_RECORDS, MAX_OPERATOR_TEXT_CHARS,
    MAX_SAME_CHANNEL_RECORDS, MAX_SELECTED_RECORDS, ConversationTurnRecord,
    NewConversationTurn, render_conversation_history,
)
from .sqlite_store import SQLiteConversationHistoryStore
from .store import ConversationHistoryStore

__all__ = [
    "MAX_ASSISTANT_TEXT_CHARS", "MAX_CROSS_CHANNEL_RECORDS",
    "MAX_OPERATOR_TEXT_CHARS", "MAX_SAME_CHANNEL_RECORDS", "MAX_SELECTED_RECORDS",
    "ConversationHistoryStore", "ConversationTurnRecord", "NewConversationTurn",
    "SQLiteConversationHistoryStore", "render_conversation_history",
]
