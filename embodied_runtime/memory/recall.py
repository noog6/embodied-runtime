"""Bounded, read-only projection of durable memory for cognition."""

from dataclasses import dataclass
import unicodedata

from .store import PersistentMemoryStore

MAX_RECALL_QUERY_CHARS = 256
MAX_RECALL_ENTITIES = 5
MAX_RECALL_MEMORIES_PER_ENTITY = 8
MAX_RECALL_FIELD_CHARS = 1000
MAX_RECALL_OUTPUT_CHARS = 12000


@dataclass(frozen=True, slots=True)
class RecalledLink:
    entity_id: int
    canonical_name: str | None
    role: str


@dataclass(frozen=True, slots=True)
class RecalledMemory:
    id: int
    kind: str
    summary: str
    predicate: str | None
    value_text: str | None
    source_kind: str | None
    source_label: str | None
    confidence: float | None
    created_at: str
    observed_at: str | None
    links: tuple[RecalledLink, ...]


@dataclass(frozen=True, slots=True)
class RecalledEntity:
    id: int
    entity_type: str
    canonical_name: str
    memories: tuple[RecalledMemory, ...]
    memories_truncated: bool


@dataclass(frozen=True, slots=True)
class MemoryRecallResult:
    query: str
    matched_entities: int
    entities: tuple[RecalledEntity, ...]
    truncated: bool

    @property
    def result(self) -> str:
        if self.matched_entities == 0:
            return "none"
        return "exact" if self.matched_entities == 1 else "ambiguous"

    def render(self) -> str:
        lines = [
            "Persistent memory recall",
            "The following is durable historical memory, not current sensor evidence.",
            f"  query: {self.query}",
            f"  result: {self.result}",
            f"  matched_entities: {self.matched_entities}",
            f"  truncated: {str(self.truncated).lower()}",
        ]
        for entity in self.entities:
            lines.extend(("", f"ENT{entity.id}", f"  type: {entity.entity_type}",
                          f"  name: {entity.canonical_name}"))
            if entity.memories_truncated:
                lines.append("  memories_truncated: true")
            for memory in entity.memories:
                lines.extend(("", f"  MEM{memory.id} {memory.kind}",
                              f"    summary: {memory.summary}"))
                for label, value in (("predicate", memory.predicate),
                                     ("value", memory.value_text),
                                     ("source_kind", memory.source_kind),
                                     ("source_label", memory.source_label),
                                     ("confidence", memory.confidence),
                                     ("created_at", memory.created_at),
                                     ("observed_at", memory.observed_at)):
                    if value is not None:
                        lines.append(f"    {label}: {value}")
                if memory.links:
                    lines.append("    links:")
                    for link in memory.links:
                        name = link.canonical_name or "[unresolved entity]"
                        lines.append(f"      ENT{link.entity_id} {name} role={link.role}")
        rendered = "\n".join(lines)
        if len(rendered) <= MAX_RECALL_OUTPUT_CHARS:
            return rendered
        marker = "\n  output_truncated: true"
        return rendered[:MAX_RECALL_OUTPUT_CHARS - len(marker)] + marker


class MemoryRecallProjector:
    """Read canonical records and expose a small deterministic historical view."""

    def __init__(self, store: PersistentMemoryStore) -> None:
        self._store = store

    def recall(self, query: object) -> MemoryRecallResult:
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        query = unicodedata.normalize("NFKC", query)
        if any(unicodedata.category(character) == "Cc" for character in query):
            raise ValueError("query must not contain control characters")
        query = " ".join(query.split())
        if not query:
            raise ValueError("query must not be empty")
        if len(query) > MAX_RECALL_QUERY_CHARS:
            raise ValueError(f"query must be at most {MAX_RECALL_QUERY_CHARS} characters")

        matches = self._store.find_entities_exact(query)
        selected = matches[:MAX_RECALL_ENTITIES]
        entities: list[RecalledEntity] = []
        truncated = len(matches) > len(selected)
        for entity in selected:
            active = tuple(memory for memory in self._store.list_memories_for_entity(entity.id)
                           if memory.record.status == "active")
            # IDs are monotonically allocated. Prefer the newest IDs, while rendering
            # the selected subset in ascending ID order for stable chronology.
            memories = active[-MAX_RECALL_MEMORIES_PER_ENTITY:]
            memory_truncated = len(active) > len(memories)
            truncated = truncated or memory_truncated
            projected: list[RecalledMemory] = []
            for stored in memories:
                record = stored.record
                links_list: list[RecalledLink] = []
                for link in stored.links:
                    resolved = self._store.get_entity(link.entity_id)
                    links_list.append(RecalledLink(
                        link.entity_id,
                        resolved.canonical_name if resolved is not None else None,
                        link.role,
                    ))
                links = tuple(links_list)
                projected.append(RecalledMemory(
                    record.id, record.kind, _bounded(record.summary), record.predicate,
                    _optional_bounded(record.value_text), record.source_kind,
                    _optional_bounded(record.source_label), record.confidence,
                    record.created_at.isoformat(),
                    record.observed_at.isoformat() if record.observed_at else None, links,
                ))
            entities.append(RecalledEntity(
                entity.id, entity.entity_type, _bounded(entity.canonical_name),
                tuple(projected), memory_truncated,
            ))
        return MemoryRecallResult(query, len(matches), tuple(entities), truncated)


def _bounded(value: str) -> str:
    if len(value) <= MAX_RECALL_FIELD_CHARS:
        return value
    return value[:MAX_RECALL_FIELD_CHARS - 1] + "…"


def _optional_bounded(value: str | None) -> str | None:
    return None if value is None else _bounded(value)
