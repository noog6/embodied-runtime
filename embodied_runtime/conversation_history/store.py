"""Application-facing durable conversation-history contract."""

from typing import Protocol
from datetime import datetime

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

    def search(self, query: str, *, limit: int = 5, exclude_ids=(),
               exclude_requests=()) -> dict: ...

    def discover_requests(self, *, work_selector: str | None = None,
                          before: str | None = None, limit: int = 5) -> dict: ...

    def inspect_request(self, selector: str) -> dict: ...

    def begin_request(self, request_id: str, session_id: str, accepted_at: datetime,
                      channel: InteractionChannel, mode: str) -> None: ...

    def prepare_response(self, request_id: str, prepared_at: datetime) -> None: ...

    def link_work(self, request_id: str, kind: str, job_id: int,
                  run_id: int | None) -> None: ...

    def begin_delivery(self, attempt_id: str, request_id: str, attempted_at: datetime,
                       channel: InteractionChannel, kind: str,
                       output_variant: str = 'authored_output',
                       report_source: dict | None = None) -> None: ...

    def finish_delivery(self, attempt_id: str, outcome_recorded_at: datetime, status: str,
                        provider_reference: str | None, failure: str | None) -> None: ...

    def close(self) -> None: ...
