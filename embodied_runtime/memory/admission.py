"""Deterministic, operator-grounded admission to persistent memory."""

from dataclasses import dataclass
import re
import unicodedata

from .model import NewMemoryLink, NewMemoryPayload
from .store import PersistentMemoryStore

_KINDS = frozenset(("fact", "preference", "relationship"))
_TOKEN = re.compile(r"^[^\W\d][\w-]*$", re.UNICODE)


@dataclass(frozen=True, slots=True)
class MemoryAdmissionProposal:
    subject: str
    kind: str
    predicate: str
    value: str
    evidence: str
    related_entity: str | None = None
    related_role: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryAdmissionResult:
    status: str
    admission: str | None = None
    entity: str | None = None
    memory: str | None = None
    error: str | None = None
    conflicts: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"status": self.status}
        for key in ("admission", "entity", "memory", "error"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        if self.conflicts:
            result["conflicts"] = list(self.conflicts)
        return result


class MemoryAdmission:
    """Validate and atomically write at most one bounded durable memory."""

    def __init__(self, store: PersistentMemoryStore) -> None:
        self._store = store

    def admit(
        self, proposal: MemoryAdmissionProposal, *, current_utterance: str,
        source_label: str | None = None,
    ) -> MemoryAdmissionResult:
        try:
            subject_name = _text(proposal.subject, "subject", 256)
            kind = _text(proposal.kind, "kind", 32)
            if kind not in _KINDS:
                raise ValueError("kind must be fact, preference, or relationship")
            predicate = _token(proposal.predicate, "predicate", 64).casefold()
            value = _text(proposal.value, "value", 500)
            evidence = _text(proposal.evidence, "evidence", 1000)
            utterance_key = _normalized(current_utterance, "current operator utterance")
            evidence_key = _normalized(evidence, "evidence")
            value_key = _normalized(value, "value")
            if not _contains_grounded_phrase(utterance_key, evidence_key):
                return _rejected("operator evidence was not found in current utterance")
            if not _contains_grounded_phrase(evidence_key, value_key):
                return _rejected("operator evidence does not support proposed value")
            if not _contains_grounded_phrase(
                evidence_key, _normalized(subject_name, "subject")
            ):
                return _rejected("operator evidence does not support proposed subject")

            has_entity = proposal.related_entity is not None
            has_role = proposal.related_role is not None
            if has_entity != has_role:
                return _rejected("related_entity and related_role must appear together")
            if has_entity and kind != "relationship":
                return _rejected("related entity is allowed only for relationship memories")

            subjects = self._store.find_entities_exact(subject_name)
            if not subjects:
                return _rejected("persistent-memory subject was not found")
            if len(subjects) != 1:
                return _rejected("persistent-memory subject is ambiguous")
            subject = subjects[0]
            links = [NewMemoryLink(subject.id, "subject")]
            if has_entity:
                related_name = _text(proposal.related_entity, "related_entity", 256)
                related_role = _token(proposal.related_role, "related_role", 64).casefold()
                related_key = _normalized(related_name, "related_entity")
                if not _contains_grounded_phrase(evidence_key, related_key):
                    return _rejected("operator evidence does not support related entity")
                if value_key != related_key:
                    return _rejected("relationship value must equal related entity")
                related = self._store.find_entities_exact(related_name)
                if not related:
                    return _rejected("related persistent-memory entity was not found")
                if len(related) != 1:
                    return _rejected("related persistent-memory entity is ambiguous")
                links.append(NewMemoryLink(related[0].id, related_role))

            link_set = frozenset(
                (link.entity_id, _normalized(link.role, "link role")) for link in links
            )
            conflicts: list[str] = []
            duplicate = None
            for existing in self._store.list_memories_for_entity(subject.id):
                record = existing.record
                if record.status != "active" or not any(
                    link.entity_id == subject.id and
                    _normalized(link.role, "stored link role") == "subject"
                    for link in existing.links
                ):
                    continue
                existing_links = frozenset(
                    (link.entity_id, _normalized(link.role, "stored link role"))
                    for link in existing.links
                )
                existing_predicate = (
                    _normalized(record.predicate, "stored predicate")
                    if record.predicate is not None else None
                )
                same_relationship = (
                    kind == "relationship" and has_entity and
                    record.kind == kind and existing_predicate == predicate and
                    existing_links == link_set
                )
                same_value = (
                    record.value_text is not None and
                    _normalized(record.value_text, "stored value") == value_key
                )
                if ((same_relationship or (
                        record.kind == kind and existing_predicate == predicate and
                        same_value)) and existing_links == link_set):
                    duplicate = duplicate or record
                if (kind in ("fact", "preference") and
                        record.kind in ("fact", "preference") and
                        existing_predicate == predicate and record.value_text is not None and
                        _normalized(record.value_text, "stored value") != value_key):
                    conflicts.append(record.identity)
            if conflicts:
                return MemoryAdmissionResult(
                    "rejected", error="conflicting active persistent memory",
                    conflicts=tuple(conflicts[:8]),
                )
            if duplicate is not None:
                return MemoryAdmissionResult(
                    "applied", "duplicate", subject.identity, duplicate.identity
                )

            stored = self._store.create_memory(
                kind, evidence, predicate=predicate, value_text=value,
                source_kind="operator_statement", source_label=source_label,
                links=tuple(links), payloads=(
                    NewMemoryPayload("text", "text/plain", evidence),
                ),
            )
            return MemoryAdmissionResult(
                "applied", "created", subject.identity, stored.record.identity
            )
        except (TypeError, ValueError) as error:
            return _rejected(str(error))


def _rejected(error: str) -> MemoryAdmissionResult:
    return MemoryAdmissionResult("rejected", error=error)


def _normalized(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    value = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"{label} must not contain control characters")
    normalized = " ".join(value.split()).casefold()
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    return normalized


def _contains_grounded_phrase(container: str, phrase: str) -> bool:
    """Match a normalized phrase without embedding its alphanumeric edges."""
    start = 0
    while True:
        index = container.find(phrase, start)
        if index < 0:
            return False
        end = index + len(phrase)
        left_embedded = index > 0 and phrase[0].isalnum() and container[index - 1].isalnum()
        right_embedded = (
            end < len(container) and phrase[-1].isalnum() and container[end].isalnum()
        )
        if not left_embedded and not right_embedded:
            return True
        start = index + 1


def _text(value: object, label: str, limit: int) -> str:
    normalized = _normalized(value, label)
    assert isinstance(value, str)
    display = " ".join(unicodedata.normalize("NFKC", value).split())
    if len(display) > limit:
        raise ValueError(f"{label} must be at most {limit} characters")
    assert normalized
    return display


def _token(value: object, label: str, limit: int) -> str:
    value = _text(value, label, limit)
    if _TOKEN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a simple identifier")
    return value
