"""Best-effort evidence recording; never a send/retry or lifecycle authority."""

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, asdict
from datetime import datetime
import logging
from uuid import uuid4

from embodied_runtime.interaction import InteractionChannel, ReportSource
from .store import ConversationHistoryStore

LOGGER = logging.getLogger(__name__)
CURRENT_REQUEST: ContextVar['RequestEvidence | None'] = ContextVar('request_evidence', default=None)


@dataclass(frozen=True, slots=True)
class RequestEvidence:
    recorder: 'ContinuityRecorder'
    id: str
    owner_task: object | None = None


class AuthoredResponse(str):
    """Plain text carrying exact correlation to its authoring request in-process."""

    def __new__(cls, text: str, evidence: RequestEvidence):
        result = super().__new__(cls, text)
        result.evidence = evidence
        return result

    def with_text(self, text: str) -> 'AuthoredResponse':
        return AuthoredResponse(text, self.evidence)


class ContinuityRecorder:
    def __init__(self, store: ConversationHistoryStore | None, session_id: str,
                 clock: Callable[[], datetime]) -> None:
        self.store = store
        self.session_id = session_id
        self.clock = clock

    def write(self, operation: str, *args):
        if self.store is None:
            return None
        try:
            return getattr(self.store, operation)(*args)
        except Exception as error:
            LOGGER.warning('[CONTINUITY] operation=%s status=unavailable error=%s',
                           operation, type(error).__name__)
            return None

    def begin(self, channel: InteractionChannel, mode: str = 'dialogue') -> RequestEvidence:
        evidence = RequestEvidence(self, 'REQ' + uuid4().hex, _current_task())
        self.write('begin_request', evidence.id, self.session_id, self.clock(), channel, mode)
        return evidence

    def prepared(self, evidence: RequestEvidence) -> None:
        self.write('prepare_response', evidence.id, self.clock())


def _current_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def current_request() -> RequestEvidence | None:
    # ContextVars propagate to newly created tasks; causal ownership must not.
    evidence = CURRENT_REQUEST.get()
    return evidence if evidence is not None and evidence.owner_task is _current_task() else None


def link_work(kind: str, job_id: int, run_id: int | None = None) -> None:
    evidence = current_request()
    if evidence is not None:
        evidence.recorder.write('link_work', evidence.id, kind, job_id, run_id)


class DeliveryAttempt:
    """Persist intent before entering a transport; missing confirmation is unknown."""

    def __init__(self, evidence: RequestEvidence | None, channel: InteractionChannel,
                 kind: str, output_variant: str = 'authored_output',
                 report_source: ReportSource | None = None) -> None:
        self.evidence = evidence
        self.id = 'DEL' + uuid4().hex
        if evidence is not None:
            evidence.recorder.write('begin_delivery', self.id, evidence.id,
                                    evidence.recorder.clock(), channel, kind, output_variant,
                                    asdict(report_source) if report_source is not None else None)

    def finish(self, status: str, *, provider_reference: str | None = None,
               failure: str | None = None) -> None:
        if self.evidence is not None:
            self.evidence.recorder.write('finish_delivery', self.id,
                self.evidence.recorder.clock(), status, provider_reference, failure)


def delivery_attempt(text: str | None = None, *, channel: InteractionChannel,
                     kind: str = 'reply', output_variant: str = 'authored_output',
                     report_source: ReportSource | None = None) -> DeliveryAttempt:
    evidence = getattr(text, 'evidence', None) if text is not None else current_request()
    return DeliveryAttempt(evidence, channel, kind, output_variant, report_source)
