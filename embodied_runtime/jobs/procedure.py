"""Private, bounded suggested methods; no executable semantics or authority."""

from dataclasses import asdict, dataclass
import json

from .focus import focus_text

CONTENT_LIMITS = dict(name=80, purpose=240, applicability=400,
                      required_capabilities=300, steps=800, verification=400,
                      stopping_conditions=400)
MAX_PROCEDURE_CONTENT_JSON_CHARS = 20_000
MAX_PROCEDURE_PROJECTION_CHARS = 24_000
MAX_PROCEDURE_RUNS = 100
BASES = ('wake_event', 'acquisition_1', 'acquisition_2', 'effect_1', 'effect_2')


@dataclass(frozen=True, slots=True)
class ProcedureProposal:
    name: str
    purpose: str
    applicability: str
    required_capabilities: str
    steps: str
    verification: str
    stopping_conditions: str
    basis: str | None = None

    def __post_init__(self) -> None:
        for field, limit in CONTENT_LIMITS.items():
            object.__setattr__(self, field, focus_text(getattr(self, field), field, limit))
        if self.basis is not None and self.basis not in BASES:
            raise ValueError('unsupported procedure evidence reference')
        if len(json.dumps(asdict(self), ensure_ascii=True)) > MAX_PROCEDURE_CONTENT_JSON_CHARS:
            raise ValueError('procedure authored content exceeds 20000 serialized characters')

    @classmethod
    def parse(cls, value: object) -> 'ProcedureProposal | None':
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {*CONTENT_LIMITS, 'basis'}:
            raise ValueError('procedure candidate requires exactly the documented content fields and basis')
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ProcedureFeedback:
    version: int
    response: str
    reason: str | None = None
    improvement: str | None = None

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version < 1:
            raise ValueError('feedback requires an exact positive procedure version')
        if self.response not in ('followed', 'adapted', 'declined'):
            raise ValueError('procedure response must be followed, adapted, or declined')
        for field in ('reason', 'improvement'):
            if getattr(self, field) is not None:
                object.__setattr__(self, field, focus_text(getattr(self, field), field, 300))
        if self.response == 'declined' and self.reason is None:
            raise ValueError('declining a procedure requires a reason')

    @classmethod
    def parse(cls, value: object) -> 'ProcedureFeedback | None':
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {'version', 'response', 'reason', 'improvement'}:
            raise ValueError('invalid procedure feedback fields')
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ProcedureRunSnapshot:
    definition_identity: str
    version: int | None
    section: str | None

    @classmethod
    def capture(cls, evidence: dict) -> 'ProcedureRunSnapshot':
        record = evidence['snapshot']
        return cls(evidence['definition_identity'], record['version'] if record else None,
                   render_procedure(record) if record else None)


PROCEDURE_GUIDANCE = (
    'Job procedure: operator-enabled suggested method, not verified current-world truth. '
    'Consider applicability and preconditions before following it. Current Job responsibility, '
    'operator instructions, runtime authority and fresh evidence control. Use only offered '
    'capabilities and existing permissions; report missing capabilities honestly. '
    'These instructions execute nothing, grant no authority, reserve no resources and expand '
    'no budgets or schedules. Reading them does not refresh evidence age. A completed source '
    'Run establishes provenance, not correctness. An operator source reference does not prove '
    'the authored instructions appeared in that source. Report following, adapting or declining '
    'separately from the independently evaluated outcome; completion does not prove effectiveness.'
)


def render_procedure(record: dict) -> str:
    text = PROCEDURE_GUIDANCE + '\n' + json.dumps(record, ensure_ascii=True, sort_keys=True)
    if len(text) > MAX_PROCEDURE_PROJECTION_CHARS:
        raise ValueError('procedure projection exceeds bounded limit')
    return text


PROCEDURE_CANDIDATE_SCHEMA = {'anyOf': [{'type': 'null'}, {
    'type': 'object', 'properties': {
        **{key: {'type': 'string', 'minLength': 1, 'maxLength': limit}
           for key, limit in CONTENT_LIMITS.items()},
        'basis': {'type': ['string', 'null'], 'enum': [*BASES, None]},
    }, 'required': [*CONTENT_LIMITS, 'basis'], 'additionalProperties': False,
}], 'description': 'Optional reusable lesson, not an incident narrative. Completed opted-in outcomes only; proposal requires operator activation.'}
PROCEDURE_FEEDBACK_SCHEMA = {'anyOf': [{'type': 'null'}, {
    'type': 'object', 'properties': {
        'version': {'type': 'integer', 'minimum': 1},
        'response': {'type': 'string', 'enum': ['followed', 'adapted', 'declined']},
        'reason': {'type': ['string', 'null'], 'maxLength': 300},
        'improvement': {'type': ['string', 'null'], 'maxLength': 300},
    }, 'required': ['version', 'response', 'reason', 'improvement'], 'additionalProperties': False,
}]}

EXPERIMENT_DESCRIPTION = (
    'Perform one bounded read-only runtime-health assessment using existing inspect_self '
    'capabilities and current runtime context. Consider runtime/platform health and available '
    'diagnostics; distinguish current observations from historical claims. Report observations, '
    'limitations (including unavailable sensors or capabilities), and one useful next measurement. '
    'Do useful work even without a procedure. Do not request hardware actions, delivery, writes, '
    'schedules, retries or notifications. Complete when this bounded assessment is reported; '
    'do not claim overall health without supporting evidence. On an accepted completed outcome, '
    'optionally propose one concise reusable investigation method as procedure_candidate; '
    'capture a lesson with applicability, branches, verification and stopping/defer conditions, '
    'not an incident narrative. Proposals require operator review and activation. If supplied '
    'a procedure, consider applicability and optionally report exact-version procedure_feedback.'
)
