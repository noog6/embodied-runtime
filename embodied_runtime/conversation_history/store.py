"""Application-facing durable conversation-history contract."""

from typing import Protocol

from embodied_runtime.interaction import InteractionChannel
from .model import (
    MAX_CROSS_CHANNEL_RECORDS, MAX_SAME_CHANNEL_RECORDS,
    ConversationTurnRecord, NewConversationTurn,
)


class ConversationHistoryStore(Protocol):
    def append(self, turn: NewConversationTurn) -> ConversationTurnRecord: ...

    def select_prior_session(
        self, current_session_id: str, current_channel: InteractionChannel,
        *, same_channel_limit: int = MAX_SAME_CHANNEL_RECORDS,
        cross_channel_limit: int = MAX_CROSS_CHANNEL_RECORDS,
    ) -> tuple[ConversationTurnRecord, ...]: ...

    def close(self) -> None: ...
