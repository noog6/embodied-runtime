from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import (
    RECALL_MEMORY_TOOL, ApplicationOptions, RobotApplication,
)
from embodied_runtime.attention import AttentionStimulus, concern_for_stimulus
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.memory import (
    MAX_RECALL_MEMORIES_PER_ENTITY, MAX_RECALL_OUTPUT_CHARS,
    MemoryRecallProjector, NewMemoryLink, NewMemoryPayload, SQLiteMemoryStore,
)
from embodied_runtime.observations import SemanticObservation
from embodied_runtime.profile import RobotProfile
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


def add_memory(store, entity, summary, **fields):
    return store.create_memory(
        fields.pop("kind", "fact"), summary,
        links=(NewMemoryLink(entity.id, "subject"),),
        payloads=(NewMemoryPayload("text", "text/plain", summary),), **fields,
    )


class MemoryRecallProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SQLiteMemoryStore(
            Path(temporary.name) / "memory.sqlite3",
            clock=lambda: datetime(2026, 9, 11, 13, 36, 50, tzinfo=UTC),
        )
        self.addCleanup(self.store.close)

    def test_exact_alias_normalization_no_fuzzy_and_projection_fields(self):
        nick = self.store.create_entity("person", "Nick")
        gordon = self.store.create_entity("object", "Gordon")
        self.store.add_entity_alias(gordon.id, "plush seal")
        memory = self.store.create_memory(
            "relationship", "Gordon is Nick's white stuffed seal.",
            links=(NewMemoryLink(nick.id, "owner"),
                   NewMemoryLink(gordon.id, "subject")),
            payloads=(NewMemoryPayload("text", "text/plain", "duplicate payload"),),
            predicate="relationship", value_text="white stuffed seal",
            source_kind="operator_statement", source_label="Nick",
            confidence=0.72,
            observed_at=datetime(2026, 9, 10, tzinfo=UTC),
        )
        projector = MemoryRecallProjector(self.store)
        for query in ("Gordon", "  GORDON ", "PLUSH   SEAL"):
            result = projector.recall(query)
            self.assertEqual(result.entities[0].id, gordon.id)
            self.assertEqual(result.entities[0].memories[0].id, memory.record.id)
        self.assertEqual(projector.recall("Gordan").result, "none")
        projected = projector.recall("Gordon").entities[0].memories[0]
        self.assertEqual((projected.predicate, projected.value_text),
                         ("relationship", "white stuffed seal"))
        self.assertEqual((projected.source_kind, projected.source_label),
                         ("operator_statement", "Nick"))
        self.assertEqual(projected.confidence, 0.72)
        self.assertIn("confidence: 0.72", projector.recall("Gordon").render())
        self.assertEqual(projected.observed_at, "2026-09-10T00:00:00+00:00")
        self.assertEqual([(link.entity_id, link.canonical_name, link.role)
                          for link in projected.links],
                         [(nick.id, "Nick", "owner"),
                          (gordon.id, "Gordon", "subject")])
        self.assertNotIn("duplicate payload", projector.recall("Gordon").render())

    def test_ambiguity_and_recent_memory_bound_are_deterministic(self):
        first = self.store.create_entity("object", "First Gordon")
        second = self.store.create_entity("person", "Second Gordon")
        self.store.add_entity_alias(first.id, "Gordon")
        self.store.add_entity_alias(second.id, "Gordon")
        ids = [add_memory(self.store, first, f"fact {index}").record.id
               for index in range(MAX_RECALL_MEMORIES_PER_ENTITY + 2)]
        result = MemoryRecallProjector(self.store).recall("Gordon")
        self.assertEqual(result.result, "ambiguous")
        self.assertEqual([entity.id for entity in result.entities], [first.id, second.id])
        self.assertEqual([memory.id for memory in result.entities[0].memories],
                         ids[-MAX_RECALL_MEMORIES_PER_ENTITY:])
        self.assertTrue(result.truncated)
        self.assertIn("memories_truncated: true", result.render())

    def test_query_validation(self):
        entity = self.store.create_entity("person", "Nick Smith")
        self.store.add_entity_alias(entity.id, "Ｎｉｃｋ　Ｓｍｉｔｈ")
        projector = MemoryRecallProjector(self.store)
        for query in (None, "", "x" * 257):
            with self.subTest(query=query), self.assertRaises((TypeError, ValueError)):
                projector.recall(query)
        self.assertEqual(projector.recall("  Nick   Smith  ").entities[0].id,
                         entity.id)
        self.assertEqual(projector.recall("Ｎｉｃｋ　Ｓｍｉｔｈ").entities[0].id,
                         entity.id)
        for query in ("Nick\tSmith", "Nick\nSmith", "Nick\x00"):
            with self.subTest(query=query), self.assertRaisesRegex(
                ValueError, "control characters"
            ):
                projector.recall(query)

    def test_rendered_output_has_deterministic_hard_bound(self):
        entity = self.store.create_entity("project", "Large")
        text = "x" * 1000
        for index in range(MAX_RECALL_MEMORIES_PER_ENTITY):
            add_memory(self.store, entity, f"{index}{text}",
                       value_text=text, source_label=text)
        result = MemoryRecallProjector(self.store).recall("Large")
        first = result.render()
        self.assertLessEqual(len(first), MAX_RECALL_OUTPUT_CHARS)
        self.assertIn("output_truncated: true", first)
        self.assertEqual(first, result.render())


class RecallBackend(TextCognitionBackend):
    identifier = "recall-script"

    def __init__(self, calls):
        self.calls = list(calls)
        self.requests = []
        self.results = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, **kwargs):
        self.requests.append((instructions, tuple(tool.name for tool in tools)))
        if self.calls:
            query = self.calls.pop(0)
            self.results.append(await tool_executor(CognitionToolCall(
                "recall_memory", json.dumps({"query": query})
            )))
            return "provisional"
        return "Yes. I remember Gordon."


class ThreeRecallBackend(RecallBackend):
    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, **kwargs):
        self.requests.append((instructions, tuple(tool.name for tool in tools)))
        if self.calls:
            query = self.calls.pop(0)
            self.results.append(await tool_executor(CognitionToolCall(
                "recall_memory", json.dumps({"query": query})
            )))
            return "provisional"
        return "final"


class AutonomousRecallBackend(TextCognitionBackend):
    identifier = "autonomous-recall-script"

    def __init__(self, *, replace_goal=False):
        self.app = None
        self.replace_goal = replace_goal
        self.requests = []
        self.results = []
        self.episode_ids = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, **kwargs):
        self.requests.append((instructions, tuple(tool.name for tool in tools)))
        self.episode_ids.append(self.app.episode_coordinator.current.id)
        if len(self.requests) == 1:
            if self.replace_goal:
                self.app.resolve_goal("cancelled")
                self.app.set_goal("replacement goal")
            self.results.append(await tool_executor(CognitionToolCall(
                "recall_memory", '{"query": "Gordon"}'
            )))
            return "provisional"
        return "terminal"


class CountingSQLiteMemoryStore(SQLiteMemoryStore):
    def __init__(self, path):
        super().__init__(path)
        self.exact_lookups = 0

    def find_entities_exact(self, name):
        self.exact_lookups += 1
        return super().find_entities_exact(name)


class FailingProjector:
    def __init__(self):
        self.calls = 0

    def recall(self, query):
        self.calls += 1
        raise RuntimeError("memory backend unavailable")


class MemoryRecallIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_operator_recall_continues_same_episode_and_is_read_only(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        nick = store.create_entity("person", "Nick")
        gordon = store.create_entity("object", "Gordon")
        store.add_entity_alias(gordon.id, "stuffed seal")
        store.add_entity_alias(gordon.id, "plush seal")
        add_memory(store, nick, "Nick prefers vi.", predicate="preferred_editor",
                   value_text="vi")
        relationship = store.create_memory(
            "relationship", "Gordon is Nick's white stuffed seal.",
            links=(NewMemoryLink(nick.id, "owner"), NewMemoryLink(gordon.id, "subject")),
            payloads=(NewMemoryPayload("text", "text/plain", "relationship"),),
        )
        backend = RecallBackend(["Gordon"])
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(), ApplicationOptions(),
            platform_provider=Platform(), cognition_backend=backend,
            persistent_memory_store=store,
        )
        await app.start()
        before = store.list_memories_for_entity(gordon.id)
        self.assertEqual(await app.request_cognition("Do you know who Gordon is?"),
                         "Yes. I remember Gordon.")
        self.assertEqual(len(backend.requests), 2)
        self.assertTrue(all("id: E1" in request[0] for request in backend.requests))
        self.assertIn("acquisitions_remaining: 1", backend.requests[1][0])
        self.assertIn("ENT2", backend.results[0].output)
        self.assertIn(f"MEM{relationship.record.id}", backend.results[0].output)
        self.assertEqual(len(app.working_memory.snapshot()), 1)
        self.assertEqual(app.working_memory.snapshot()[0].assistant_text,
                         "Yes. I remember Gordon.")
        self.assertEqual(store.list_memories_for_entity(gordon.id), before)
        self.assertEqual(app.episode_coordinator.last.id, 1)
        await app.stop()

    async def test_miss_consumes_slot_without_fuzzy_fallback(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        store.create_entity("object", "Gordon")
        backend = RecallBackend(["Gardan"])
        app = RobotApplication(RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=Platform(), cognition_backend=backend,
            persistent_memory_store=store)
        await app.start(); await app.request_cognition("who?")
        self.assertIn("result: none", backend.results[0].output)
        self.assertIn("acquisitions_remaining: 1", backend.requests[1][0])
        self.assertEqual(store.find_entities_exact("Gordon")[0].id, 1)
        await app.stop()

    async def test_unconfigured_store_does_not_offer_recall(self):
        backend = RecallBackend([])
        app = RobotApplication(RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=Platform(), cognition_backend=backend)
        await app.start(); await app.request_cognition("hello")
        self.assertNotIn("recall_memory", backend.requests[0][1])
        self.assertNotIn("recall_memory", backend.requests[0][0])
        await app.stop()

    async def test_configured_store_offers_recall_with_policy(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        backend = RecallBackend([])
        app = RobotApplication(RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=Platform(), cognition_backend=backend,
            persistent_memory_store=store)
        await app.start(); await app.request_cognition("hello")
        self.assertIn("recall_memory", backend.requests[0][1])
        self.assertIn("recall_memory", backend.requests[0][0])
        self.assertIn("historical stored knowledge", RECALL_MEMORY_TOOL.description)
        self.assertIn("exact known entity name", RECALL_MEMORY_TOOL.description)
        await app.stop()

    async def test_third_recall_is_rejected_by_existing_episode_budget(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        store.create_entity("object", "Gordon")
        store.create_entity("person", "Nick")
        backend = ThreeRecallBackend(["Gordon", "Nick", "workbench"])
        app = RobotApplication(RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=Platform(), cognition_backend=backend,
            persistent_memory_store=store)
        await app.start(); await app.request_cognition("recall three")
        self.assertEqual(len(backend.requests), 3)
        self.assertIn("recall_memory", backend.requests[0][1])
        self.assertIn("recall_memory", backend.requests[1][1])
        self.assertNotIn("recall_memory", backend.requests[2][1])
        self.assertEqual(json.loads(backend.results[2].output)["status"], "rejected")
        self.assertIn("tool is not available", backend.results[2].output)
        self.assertEqual(len(app.working_memory.snapshot()[0].tool_outcomes), 3)
        await app.stop()

    async def test_backend_failure_consumes_one_operator_acquisition(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        gordon = store.create_entity("object", "Gordon")
        before = store.find_entities_exact("Gordon")
        backend = RecallBackend(["Gordon"])
        app = RobotApplication(RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=Platform(), cognition_backend=backend,
            persistent_memory_store=store)
        failing = FailingProjector()
        app._memory_recall = failing
        await app.start()
        self.assertEqual(await app.request_cognition("remember Gordon"),
                         "Yes. I remember Gordon.")
        self.assertEqual(failing.calls, 1)
        self.assertEqual(json.loads(backend.results[0].output), {
            "error": "memory backend unavailable", "status": "rejected",
        })
        self.assertEqual(len(backend.requests), 2)
        self.assertIn("id: E1", backend.requests[1][0])
        self.assertIn("acquisitions_remaining: 1", backend.requests[1][0])
        self.assertEqual(store.find_entities_exact("Gordon"), before)
        self.assertEqual(store.get_entity(gordon.id), gordon)
        await app.stop()

    async def test_autonomous_recall_uses_existing_episode_and_budget(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = CountingSQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        gordon = store.create_entity("object", "Gordon")
        add_memory(store, gordon, "Gordon is remembered.")
        before = store.list_memories_for_entity(gordon.id)
        backend = AutonomousRecallBackend()
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True), platform_provider=Platform(),
            cognition_backend=backend, persistent_memory_store=store,
        )
        backend.app = app
        await app.start()
        goal = app.set_goal("consider remembered objects")
        stimulus = AttentionStimulus(SemanticObservation("test", "test", ()))
        episode = app.episode_coordinator.try_start(
            stimulus.kind, stimulus.source, concern_for_stimulus(stimulus), goal.id
        )
        self.assertIsNotNone(episode)
        initial_effect_tools = tuple(tool.name for tool in app.effect_tools())
        await app._request_initiative(stimulus, episode)
        self.assertEqual(backend.episode_ids, [episode.id, episode.id])
        self.assertIn("recall_memory", backend.requests[0][1])
        self.assertIn("acquisitions_remaining: 1", backend.requests[1][0])
        self.assertEqual(tuple(tool.name for tool in app.effect_tools()),
                         initial_effect_tools)
        self.assertEqual(app.episode_coordinator.current, episode)
        self.assertEqual(app.episode_coordinator._next_id, 2)
        self.assertEqual(store.exact_lookups, 1)
        self.assertEqual(store.list_memories_for_entity(gordon.id), before)
        self.assertIs(app.active_goal, goal)
        app.episode_coordinator.close(episode, "handled")
        await app.stop()

    async def test_autonomous_recall_rejects_stale_bound_goal_without_lookup(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = CountingSQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        store.create_entity("object", "Gordon")
        backend = AutonomousRecallBackend(replace_goal=True)
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True), platform_provider=Platform(),
            cognition_backend=backend, persistent_memory_store=store,
        )
        backend.app = app
        await app.start()
        goal = app.set_goal("original goal")
        stimulus = AttentionStimulus(SemanticObservation("test", "test", ()))
        episode = app.episode_coordinator.try_start(
            stimulus.kind, stimulus.source, concern_for_stimulus(stimulus), goal.id
        )
        await app._request_initiative(stimulus, episode)
        self.assertEqual(store.exact_lookups, 0)
        self.assertEqual(json.loads(backend.results[0].output)["status"], "rejected")
        self.assertIn("expected active goal is no longer current",
                      backend.results[0].output)
        self.assertEqual(backend.episode_ids, [episode.id])
        self.assertEqual(app.episode_coordinator.current, episode)
        app.episode_coordinator.close(episode, "stale_goal")
        await app.stop()


if __name__ == "__main__":
    unittest.main()
