from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from embodied_runtime.memory import (
    NewMemoryLink,
    NewMemoryPayload,
    SQLiteMemoryStore,
)


NOW = datetime(2026, 9, 11, 12, 34, 56, 123456, tzinfo=UTC)


class SQLiteMemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "memory.sqlite3"

    def open_store(self) -> SQLiteMemoryStore:
        store = SQLiteMemoryStore(self.path, clock=lambda: NOW)
        self.addCleanup(store.close)
        return store

    def test_creates_versioned_normalized_schema_and_reopens_it(self) -> None:
        store = self.open_store()
        store.close()
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        self.assertTrue({
            "entities", "entity_aliases", "memories", "memory_entity_links",
            "memory_payloads",
        }.issubset(tables))
        indexes = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )}
        self.assertTrue({
            "idx_entities_normalized", "idx_aliases_normalized",
            "idx_memory_links_entity", "idx_memory_payloads_memory",
            "idx_memories_kind_status",
        }.issubset(indexes))
        reopened = SQLiteMemoryStore(self.path)
        reopened.close()

    def test_rejects_unsupported_or_unversioned_nonempty_schema(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA user_version = 2")
        connection.close()
        with self.assertRaisesRegex(RuntimeError, "unsupported.*version 2"):
            SQLiteMemoryStore(self.path)

        other = Path(self.temporary_directory.name) / "unversioned.sqlite3"
        connection = sqlite3.connect(other)
        connection.execute("CREATE TABLE unexpected (id INTEGER)")
        connection.close()
        with self.assertRaisesRegex(RuntimeError, "unversioned non-empty"):
            SQLiteMemoryStore(other)

    def test_entity_validation_identity_and_deterministic_exact_matching(self) -> None:
        store = self.open_store()
        with self.assertRaises(ValueError):
            store.create_entity("", "Gordon")
        with self.assertRaises(ValueError):
            store.create_entity("object", "  ")
        first = store.create_entity("object", " Gordon ")
        second = store.create_entity("project", "GORDON")
        self.assertEqual(first.identity, "ENT1")
        self.assertEqual(first.canonical_name, "Gordon")
        self.assertEqual(store.get_entity(first.id), first)
        self.assertEqual(
            [entity.id for entity in store.find_entities_exact("  gordon  ")],
            [first.id, second.id],
        )
        self.assertEqual(store.find_entities_exact("Gordan"), ())
        with self.assertRaises(ValueError):
            store.get_entity(0)

    def test_aliases_use_unicode_case_and_whitespace_normalization(self) -> None:
        store = self.open_store()
        gordon = store.create_entity("object", "Gordon")
        alias = store.add_entity_alias(gordon.id, "  Stuffed   Seal ")
        self.assertEqual(alias.alias, "Stuffed Seal")
        self.assertEqual(store.find_entities_exact("stuffed seal"), (gordon,))
        cafe = store.create_entity("place", "Café")
        store.add_entity_alias(cafe.id, "ＡＴＥＬＩＥＲ")
        self.assertEqual(store.find_entities_exact("atelier"), (cafe,))
        decomposed = "Cafe\N{COMBINING ACUTE ACCENT}"
        self.assertEqual(store.find_entities_exact(decomposed), (cafe,))
        with self.assertRaises(ValueError):
            store.add_entity_alias(gordon.id, " \t ")

    def test_ambiguous_alias_and_canonical_matches_are_deduplicated(self) -> None:
        store = self.open_store()
        first = store.create_entity("object", "Seal")
        second = store.create_entity("animal", "Harbor seal")
        store.add_entity_alias(first.id, "seal")
        store.add_entity_alias(second.id, "seal")
        self.assertEqual(store.find_entities_exact("SEAL"), (first, second))

    def test_memory_models_links_payloads_and_ordering(self) -> None:
        store = self.open_store()
        nick = store.create_entity("person", "Nick")
        gordon = store.create_entity("object", "Gordon")
        first = store.create_memory(
            "relationship", "Gordon belongs to Nick.",
            links=(NewMemoryLink(gordon.id, "subject"), NewMemoryLink(nick.id, "owner")),
            payloads=(
                NewMemoryPayload("text", "text/plain", "Gordon is Nick's seal."),
                NewMemoryPayload("text", "text/markdown", "**Gordon** belongs to Nick."),
            ),
            source_kind="operator_statement", source_label="Nick", confidence=0.95,
            observed_at=datetime(2026, 9, 11, 14, tzinfo=timezone(timedelta(hours=2))),
        )
        second = store.create_memory(
            "observation", "Gordon is white.",
            links=(NewMemoryLink(gordon.id, "subject"),
                   NewMemoryLink(gordon.id, "object")),
            payloads=(NewMemoryPayload("text", "text/plain; charset=utf-8", "白い seal"),),
        )
        self.assertEqual(first.record.identity, "MEM1")
        self.assertEqual([item.id for item in first.payloads], [1, 2])
        self.assertEqual(first.payloads[1].media_type, "text/markdown")
        self.assertEqual(first.record.source_kind, "operator_statement")
        self.assertEqual(first.record.source_label, "Nick")
        self.assertEqual(first.record.observed_at, datetime(2026, 9, 11, 12, tzinfo=UTC))
        self.assertEqual(first.record.created_at, NOW)
        self.assertEqual(
            [item.record.id for item in store.list_memories_for_entity(gordon.id)],
            [first.record.id, second.record.id],
        )
        self.assertEqual(
            [link.role for link in first.links], ["owner", "subject"]
        )

    def test_validates_memory_fields_confidence_ids_roles_and_payloads(self) -> None:
        store = self.open_store()
        valid_payload = (NewMemoryPayload("text", "text/plain", "content"),)
        for confidence in (-0.01, 1.01, float("nan"), True):
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                store.create_memory("fact", "summary", payloads=valid_payload,
                                    confidence=confidence)
        for kind, summary in (("", "summary"), ("fact", "  ")):
            with self.assertRaises(ValueError):
                store.create_memory(kind, summary, payloads=valid_payload)
        with self.assertRaises(ValueError):
            store.create_memory("fact", "summary", payloads=())
        with self.assertRaises(ValueError):
            store.create_memory(
                "fact", "summary", payloads=valid_payload,
                links=(NewMemoryLink(1, "not a role"),),
            )
        with self.assertRaises(ValueError):
            store.get_memory(-1)
        invalid_payloads = (
            NewMemoryPayload("text", "text/plain", None),
            NewMemoryPayload("text", "text/plain", "  "),
            NewMemoryPayload("text", "text/plain", "text", "objects/a", "abc"),
            NewMemoryPayload("object", "image/jpeg", None, "objects/a", "abc"),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                store.create_memory("fact", "summary", payloads=(payload,))

    def test_foreign_key_failure_rolls_back_whole_memory(self) -> None:
        store = self.open_store()
        with self.assertRaisesRegex(ValueError, "store constraints"):
            store.create_memory(
                "fact", "orphan candidate",
                links=(NewMemoryLink(999, "subject"),),
                payloads=(NewMemoryPayload("text", "text/plain", "orphan"),),
            )
        successful = store.create_memory(
            "fact", "first committed memory",
            payloads=(NewMemoryPayload("text", "text/plain", "committed"),),
        )
        self.assertEqual(successful.record.id, 1)
        self.assertEqual(successful.payloads[0].id, 1)

    def test_rejects_naive_persistent_timestamps(self) -> None:
        store = self.open_store()
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            store.create_memory(
                "event", "an event", observed_at=datetime(2026, 1, 1),
                payloads=(NewMemoryPayload("text", "text/plain", "event"),),
            )

    def test_gordon_survives_complete_close_and_new_store_instance(self) -> None:
        store = SQLiteMemoryStore(self.path, clock=lambda: NOW)
        nick = store.create_entity("person", "Nick")
        gordon = store.create_entity("object", "Gordon")
        store.add_entity_alias(gordon.id, "stuffed seal")
        store.add_entity_alias(gordon.id, "plush seal")
        preference = store.create_memory(
            "fact", "Nick prefers vi.", predicate="preferred_editor", value_text="vi",
            source_kind="operator_statement", source_label="Nick",
            links=(NewMemoryLink(nick.id, "subject"),),
            payloads=(NewMemoryPayload("text", "text/plain", "Nick's preferred editor is vi."),),
        )
        relationship = store.create_memory(
            "relationship", "Gordon is Nick's white stuffed seal.",
            source_kind="operator_statement", source_label="Nick", confidence=1.0,
            links=(NewMemoryLink(gordon.id, "subject"), NewMemoryLink(nick.id, "owner")),
            payloads=(NewMemoryPayload("text", "text/plain; charset=utf-8",
                                       "Gordon is Nick's white stuffed seal. 🦭"),),
        )
        entity_ids = (nick.id, gordon.id)
        memory_ids = (preference.record.id, relationship.record.id)
        payload_id = relationship.payloads[0].id
        store.close()

        reopened = SQLiteMemoryStore(self.path, clock=lambda: NOW + timedelta(days=1))
        self.addCleanup(reopened.close)
        self.assertEqual((reopened.get_entity(nick.id).id,
                          reopened.get_entity(gordon.id).id), entity_ids)
        self.assertEqual(reopened.find_entities_exact(" GORDON "), (gordon,))
        self.assertEqual(reopened.find_entities_exact("plush   seal"), (gordon,))
        restored = reopened.get_memory(relationship.record.id)
        self.assertIsNotNone(restored)
        self.assertEqual((preference.record.id, restored.record.id), memory_ids)
        self.assertEqual(restored.record.source_label, "Nick")
        self.assertEqual(restored.record.created_at, NOW)
        self.assertEqual(restored.payloads[0].id, payload_id)
        self.assertEqual(restored.payloads[0].inline_text,
                         "Gordon is Nick's white stuffed seal. 🦭")
        self.assertEqual(restored.payloads[0].media_type, "text/plain; charset=utf-8")
        self.assertEqual(
            [item.record.id for item in reopened.list_memories_for_entity(gordon.id)],
            [relationship.record.id],
        )


if __name__ == "__main__":
    unittest.main()
