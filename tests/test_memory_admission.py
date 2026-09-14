import json
from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import REMEMBER_TOOL, ApplicationOptions, RobotApplication
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.memory import (
    MemoryAdmission, MemoryAdmissionProposal, MemoryRecallProjector, NewMemoryLink,
    NewMemoryPayload, SQLiteMemoryStore,
)
from embodied_runtime.profile import RobotProfile
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


def proposal(**changes):
    values = dict(
        subject="Gordon", kind="fact", predicate="favorite_snack", value="herring",
        evidence="Gordon's favorite snack is herring",
    )
    values.update(changes)
    return MemoryAdmissionProposal(**values)


class MemoryAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        self.addCleanup(self.store.close)
        self.nick = self.store.create_entity("person", "Nick")
        self.gordon = self.store.create_entity("object", "Gordon")
        self.admission = MemoryAdmission(self.store)
        self.utterance = "Please remember that Gordon's favorite snack is herring."

    def admit(self, item=None, utterance=None):
        return self.admission.admit(
            item or proposal(), current_utterance=utterance or self.utterance,
            source_label="voice",
        )

    def test_exact_alias_normalized_evidence_provenance_and_payload(self):
        self.store.add_entity_alias(self.gordon.id, "Stuffed Seal")
        item = proposal(subject="  stuffed   seal ", value="ＨＥＲＲＩＮＧ",
                        evidence="Stuffed Seal's favorite snack is herring")
        result = self.admit(item,
                            "Please remember Stuffed Seal's favorite snack is herring.")
        self.assertEqual((result.status, result.admission, result.entity),
                         ("applied", "created", "ENT2"))
        stored = self.store.get_memory(int(result.memory[3:]))
        self.assertEqual((stored.record.kind, stored.record.predicate,
                          stored.record.value_text), ("fact", "favorite_snack", "HERRING"))
        self.assertEqual((stored.record.source_kind, stored.record.source_label,
                          stored.record.status, stored.record.observed_at,
                          stored.record.confidence),
                         ("operator_statement", "voice", "active", None, None))
        self.assertEqual([(link.entity_id, link.role) for link in stored.links],
                         [(self.gordon.id, "subject")])
        self.assertEqual(len(stored.payloads), 1)
        payload = stored.payloads[0]
        self.assertEqual((payload.payload_kind, payload.media_type, payload.inline_text,
                          payload.object_ref, payload.sha256),
                         ("text", "text/plain", item.evidence, None, None))
        self.assertEqual(stored.record.summary, item.evidence)
        self.assertNotIn("summary", REMEMBER_TOOL.parameters["properties"])

    def test_missing_and_ambiguous_subject_never_create_entity_or_memory(self):
        missing = self.admit(proposal(subject="Pixel", evidence="Pixel likes herring"),
                             "Pixel likes herring")
        self.assertEqual(missing.status, "rejected")
        self.assertEqual(missing.reason, "subject_not_found")
        self.assertEqual(self.store.find_entities_exact("Pixel"), ())
        other = self.store.create_entity("object", "Other")
        self.store.add_entity_alias(self.gordon.id, "Seal")
        self.store.add_entity_alias(other.id, "Seal")
        ambiguous = self.admit(
            proposal(subject="Seal", evidence="Seal likes herring"),
            "Seal likes herring",
        )
        self.assertEqual(ambiguous.status, "rejected")
        self.assertEqual(ambiguous.reason, "subject_ambiguous")
        self.assertEqual(self.store.list_memories_for_entity(self.gordon.id), ())

    def test_grounding_rejections_have_stable_reasons(self):
        not_verbatim = self.admit(
            proposal(evidence="Gordon likes herring"), self.utterance
        )
        unsupported_value = self.admit(proposal(value="sardines"), self.utterance)
        unsupported_subject = self.admit(
            proposal(subject="Nick", evidence="Gordon's favorite snack is herring")
        )
        self.assertEqual(not_verbatim.reason, "evidence_not_in_utterance")
        self.assertEqual(unsupported_value.reason, "value_not_supported")
        self.assertEqual(unsupported_subject.reason, "subject_not_supported")

    def test_evidence_value_controls_and_bounds_are_rejected(self):
        cases = (
            (proposal(evidence="Gordon likes sardines"), self.utterance),
            (proposal(value="sardines"), self.utterance),
            (proposal(evidence="Gordon's\nfavorite snack is herring"), self.utterance),
            (proposal(evidence="Gordon likes \x00herring"), self.utterance),
            (proposal(subject="Gordon", evidence="Nick's favorite snack is herring"),
             "Nick's favorite snack is herring"),
        )
        for item, utterance in cases:
            with self.subTest(item=item):
                self.assertEqual(self.admit(item, utterance).status, "rejected")
        self.assertEqual(self.store.list_memories_for_entity(self.gordon.id), ())

    def test_grounding_requires_phrase_boundaries(self):
        al = self.store.create_entity("person", "Al")
        subject = self.admit(
            proposal(subject="Al", value="seal", evidence="Gordon is a seal"),
            "Gordon is a seal.",
        )
        value = self.admit(
            proposal(value="red", evidence="Gordon preferred blue"),
            "Gordon preferred blue.",
        )
        self.assertEqual((subject.status, value.status), ("rejected", "rejected"))
        self.assertEqual(self.store.list_memories_for_entity(al.id), ())
        self.assertEqual(self.store.list_memories_for_entity(self.gordon.id), ())

    def test_runtime_self_fact_preserves_operator_evidence_and_explicit_name(self):
        runtime_self = self.store.create_entity("robot", "Current Robot")
        admission = MemoryAdmission(
            self.store, runtime_self_name="Current Robot"
        )
        self_referenced = proposal(
            subject="your", predicate="indicator_color", value="blue",
            evidence="your indicator color is blue",
        )
        created = admission.admit(
            self_referenced,
            current_utterance="Remember that your indicator color is blue.",
            source_label="voice",
        )
        stored = self.store.get_memory(int(created.memory[3:]))
        self.assertEqual((created.status, created.admission, created.entity),
                         ("applied", "created", runtime_self.identity))
        self.assertEqual([(link.entity_id, link.role) for link in stored.links],
                         [(runtime_self.id, "subject")])
        self.assertEqual(stored.record.summary, "your indicator color is blue")
        self.assertEqual(stored.payloads[0].inline_text,
                         "your indicator color is blue")
        self.assertNotIn("Current Robot's", stored.record.summary)

        explicit = admission.admit(
            proposal(subject="Current Robot", predicate="shutdown_voltage",
                     value="around 6.750 V",
                     evidence="Current Robot's shutdown voltage is around 6.750 V"),
            current_utterance=(
                "Remember that Current Robot's shutdown voltage is around 6.750 V."
            ),
        )
        self.assertEqual((explicit.status, explicit.admission, explicit.entity),
                         ("applied", "created", runtime_self.identity))

    def test_runtime_self_duplicate_conflict_and_channel_independence(self):
        runtime_self = self.store.create_entity("robot", "Current Robot")
        admission = MemoryAdmission(self.store, runtime_self_name="Current Robot")
        original = self.store.create_memory(
            "fact", "Current Robot voltage", predicate="shutdown_voltage",
            value_text="around 6.750 V",
            links=(NewMemoryLink(runtime_self.id, "subject"),),
            payloads=(NewMemoryPayload("text", "text/plain", "original"),),
        )
        duplicate = admission.admit(
            proposal(subject="your", predicate="shutdown_voltage",
                     value="around 6.750 V",
                     evidence="your shutdown voltage is around 6.750 V"),
            current_utterance=(
                "Remember that your shutdown voltage is around 6.750 V."
            ), source_label="console",
        )
        conflict = admission.admit(
            proposal(subject="yours", predicate="shutdown_voltage", value="7 V",
                     evidence="yours is a shutdown voltage of 7 V"),
            current_utterance="Remember that yours is a shutdown voltage of 7 V.",
            source_label="voice",
        )
        self.assertEqual((duplicate.status, duplicate.admission, duplicate.memory),
                         ("applied", "duplicate", original.record.identity))
        self.assertEqual((conflict.status, conflict.reason, conflict.conflicts),
                         ("rejected", "conflict", (original.record.identity,)))
        self.assertEqual(len(self.store.list_memories_for_entity(runtime_self.id)), 1)

    def test_runtime_self_vocabulary_is_bounded_and_excludes_operator_first_person(self):
        self.store.create_entity("robot", "Current Robot")
        admission = MemoryAdmission(self.store, runtime_self_name="Current Robot")
        for reference in ("you", "your", "yours", "yourself"):
            with self.subTest(reference=reference):
                result = admission.admit(
                    proposal(subject=reference, predicate=f"marker_{reference}",
                             value="blue", evidence=f"{reference} marker is blue"),
                    current_utterance=f"Remember that {reference} marker is blue.",
                )
                self.assertEqual(result.admission, "created")
        for reference in ("I", "me", "my", "mine", "myself", "young", "yourselfish",
                          "courtyard"):
            with self.subTest(reference=reference):
                result = admission.admit(
                    proposal(subject="Current Robot", predicate="preferred_editor",
                             value="vi", evidence=f"{reference} preferred editor is vi"),
                    current_utterance=(
                        f"Remember that {reference} preferred editor is vi."
                    ),
                )
                self.assertEqual(result.reason, "subject_not_supported")

    def test_runtime_self_requires_one_exact_persistent_entity(self):
        missing = MemoryAdmission(
            self.store, runtime_self_name="Absent Robot"
        ).admit(
            proposal(subject="your", value="blue", evidence="your color is blue"),
            current_utterance="Remember that your color is blue.",
        )
        first = self.store.create_entity("robot", "First")
        second = self.store.create_entity("robot", "Second")
        self.store.add_entity_alias(first.id, "Shared Runtime")
        self.store.add_entity_alias(second.id, "Shared Runtime")
        ambiguous = MemoryAdmission(
            self.store, runtime_self_name="Shared Runtime"
        ).admit(
            proposal(subject="yourself", value="blue",
                     evidence="yourself has color blue"),
            current_utterance="Remember that yourself has color blue.",
        )
        self.assertEqual(missing.reason, "subject_not_found")
        self.assertEqual(ambiguous.reason, "subject_ambiguous")
        self.assertEqual(self.store.find_entities_exact("Absent Robot"), ())
        self.assertEqual(self.store.list_memories_for_entity(first.id), ())
        self.assertEqual(self.store.list_memories_for_entity(second.id), ())

    def test_runtime_self_can_be_relationship_subject_only(self):
        runtime_self = self.store.create_entity("robot", "Current Robot")
        hardware = self.store.create_entity("hardware", "Example HAT")
        result = MemoryAdmission(
            self.store, runtime_self_name="Current Robot"
        ).admit(
            proposal(subject="you", kind="relationship", predicate="paired_with",
                     value="Example HAT", evidence="you are paired with Example HAT",
                     related_entity="Example HAT", related_role="paired_device"),
            current_utterance="Remember that you are paired with Example HAT.",
        )
        stored = self.store.get_memory(int(result.memory[3:]))
        self.assertEqual(result.admission, "created")
        self.assertEqual(set((link.entity_id, link.role) for link in stored.links),
                         {(runtime_self.id, "subject"),
                          (hardware.id, "paired_device")})
        self.assertEqual(stored.record.summary, "you are paired with Example HAT")

    def test_relationship_resolves_related_entity_and_requires_pair(self):
        item = proposal(
            kind="relationship", predicate="owner", value="Nick",
            evidence="Gordon belongs to Nick",
            related_entity="Nick", related_role="owner",
        )
        result = self.admit(item, "Remember that Gordon belongs to Nick.")
        stored = self.store.get_memory(int(result.memory[3:]))
        self.assertEqual(set((link.entity_id, link.role) for link in stored.links),
                         {(self.gordon.id, "subject"), (self.nick.id, "owner")})
        duplicate = self.admit(item, "Remember that Gordon belongs to Nick.")
        self.assertEqual((duplicate.admission, duplicate.memory),
                         ("duplicate", result.memory))
        for bad in (
            proposal(kind="relationship", related_entity="Nick"),
            proposal(kind="fact", related_entity="Nick", related_role="owner"),
            proposal(kind="relationship", related_entity="Missing", related_role="owner"),
        ):
            self.assertEqual(self.admit(bad).status, "rejected")
        other = self.store.create_entity("person", "Other Nick")
        self.store.add_entity_alias(self.nick.id, "Owner")
        self.store.add_entity_alias(other.id, "Owner")
        ambiguous = proposal(kind="relationship", related_entity="Owner",
                             related_role="owner", value="Owner",
                             evidence="Gordon belongs to Owner")
        self.assertEqual(self.admit(ambiguous, "Gordon belongs to Owner").status,
                         "rejected")
        mismatch = proposal(kind="relationship", value="Nick", related_entity="Other Nick",
                            related_role="owner", evidence="Gordon belongs to Nick")
        self.assertEqual(self.admit(mismatch, "Gordon belongs to Nick").status, "rejected")

    def test_duplicate_is_noop_and_fact_conflict_is_rejected(self):
        created = self.admit()
        duplicate = self.admit()
        self.assertEqual((duplicate.status, duplicate.admission, duplicate.memory),
                         ("applied", "duplicate", created.memory))
        self.assertEqual(len(self.store.list_memories_for_entity(self.gordon.id)), 1)
        conflict = self.admit(
            proposal(value="sardines", evidence="Gordon likes sardines"),
            "Gordon likes sardines"
        )
        self.assertEqual(conflict.error, "conflicting active persistent memory")
        self.assertEqual(conflict.reason, "conflict")
        self.assertEqual(conflict.conflicts, (created.memory,))
        self.assertEqual(len(self.store.list_memories_for_entity(self.gordon.id)), 1)

    def test_operator_preference_created_duplicate_and_conflict_contract(self):
        vi = proposal(
            subject="Nick", kind="preference", predicate="preferred_editor",
            value="vi", evidence="Nick prefers vi",
        )
        created = self.admit(vi, "Remember that Nick prefers vi.")
        duplicate = self.admit(vi, "Remember that Nick prefers vi.")
        emacs = proposal(
            subject="Nick", kind="preference", predicate="preferred_editor",
            value="emacs", evidence="Nick prefers emacs",
        )
        conflict = self.admit(emacs, "Remember that Nick prefers emacs.")
        self.assertEqual((created.status, created.admission), ("applied", "created"))
        self.assertEqual((duplicate.status, duplicate.admission, duplicate.memory),
                         ("applied", "duplicate", created.memory))
        self.assertEqual((conflict.status, conflict.reason), ("rejected", "conflict"))
        self.assertEqual(conflict.conflicts, (created.memory,))
        self.assertEqual(len(self.store.list_memories_for_entity(self.nick.id)), 1)

    def _assert_cross_kind_duplicate(self, existing_kind, proposed_kind):
        existing = self.store.create_memory(
            existing_kind, "Nick prefers vi", predicate="preferred_editor",
            value_text="vi", source_kind="import", source_label="fixture",
            links=(NewMemoryLink(self.nick.id, "subject"),),
            payloads=(NewMemoryPayload("text", "text/plain", "original"),),
        )
        before = self.store.get_memory(existing.record.id)
        result = self.admit(
            proposal(subject="Nick", kind=proposed_kind,
                     predicate="preferred_editor", value="vi",
                     evidence="Nick prefers vi"),
            "Remember that Nick prefers vi.",
        )
        self.assertEqual(
            (result.status, result.admission, result.entity, result.memory),
            ("applied", "duplicate", self.nick.identity, existing.record.identity),
        )
        self.assertEqual(self.store.list_memories_for_entity(self.nick.id), (before,))

    def test_existing_fact_is_duplicate_of_proposed_preference(self):
        self._assert_cross_kind_duplicate("fact", "preference")

    def test_existing_preference_is_duplicate_of_proposed_fact(self):
        self._assert_cross_kind_duplicate("preference", "fact")

    def _assert_cross_kind_conflict(self, existing_kind, proposed_kind):
        existing = self.store.create_memory(
            existing_kind, "Nick prefers vi", predicate="preferred_editor",
            value_text="vi", links=(NewMemoryLink(self.nick.id, "subject"),),
            payloads=(NewMemoryPayload("text", "text/plain", "original"),),
        )
        result = self.admit(
            proposal(subject="Nick", kind=proposed_kind,
                     predicate="preferred_editor", value="emacs",
                     evidence="Nick prefers emacs"),
            "Remember that Nick prefers emacs.",
        )
        self.assertEqual((result.status, result.reason, result.conflicts),
                         ("rejected", "conflict", (existing.record.identity,)))
        self.assertEqual(len(self.store.list_memories_for_entity(self.nick.id)), 1)

    def test_existing_fact_conflicts_with_proposed_preference(self):
        self._assert_cross_kind_conflict("fact", "preference")

    def test_existing_preference_conflicts_with_proposed_fact(self):
        self._assert_cross_kind_conflict("preference", "fact")

    def test_cross_kind_duplicate_requires_same_predicate(self):
        self.store.create_memory(
            "fact", "Nick prefers vi", predicate="default_editor", value_text="vi",
            links=(NewMemoryLink(self.nick.id, "subject"),),
            payloads=(NewMemoryPayload("text", "text/plain", "original"),),
        )
        result = self.admit(
            proposal(subject="Nick", kind="preference", predicate="preferred_editor",
                     value="vi", evidence="Nick prefers vi"),
            "Remember that Nick prefers vi.",
        )
        self.assertEqual(result.admission, "created")

    def test_cross_kind_duplicate_requires_same_direct_links(self):
        extra_link = self.store.create_memory(
            "fact", "Nick prefers vi", predicate="preferred_editor", value_text="vi",
            links=(NewMemoryLink(self.nick.id, "subject"),
                   NewMemoryLink(self.gordon.id, "context")),
            payloads=(NewMemoryPayload("text", "text/plain", "original"),),
        )
        result = self.admit(
            proposal(subject="Nick", kind="preference", predicate="preferred_editor",
                     value="vi", evidence="Nick prefers vi"),
            "Remember that Nick prefers vi.",
        )
        self.assertEqual(result.admission, "created")
        self.assertEqual(len(self.store.list_memories_for_entity(self.nick.id)), 2)
        self.assertEqual(self.store.get_memory(extra_link.record.id), extra_link)

    def test_machine_identifiers_are_casefolded(self):
        created = self.admit(proposal(predicate="FAVORITE_SNACK"))
        duplicate = self.admit(proposal(predicate="favorite_snack"))
        self.assertEqual(duplicate.memory, created.memory)
        self.assertEqual(self.store.get_memory(int(created.memory[3:])).record.predicate,
                         "favorite_snack")
        relationship = proposal(kind="relationship", predicate="OWNER", value="Nick",
                                evidence="Gordon belongs to Nick", related_entity="Nick",
                                related_role="Owner")
        result = self.admit(relationship, "Gordon belongs to Nick")
        stored = self.store.get_memory(int(result.memory[3:]))
        self.assertIn((self.nick.id, "owner"),
                      [(link.entity_id, link.role) for link in stored.links])

    def test_relationship_duplicate_uses_resolved_identity_and_normalized_roles(self):
        self.store.add_entity_alias(self.nick.id, "Sparksmith")
        first = self.admit(
            proposal(kind="relationship", predicate="owner", value="Nick",
                     evidence="Gordon belongs to Nick", related_entity="Nick",
                     related_role="owner"),
            "Gordon belongs to Nick",
        )
        alias = self.admit(
            proposal(kind="relationship", predicate="OWNER", value="Sparksmith",
                     evidence="Gordon belongs to Sparksmith",
                     related_entity="Sparksmith", related_role="Owner"),
            "Gordon belongs to Sparksmith",
        )
        self.assertEqual((alias.admission, alias.memory), ("duplicate", first.memory))
        self.assertEqual(self.store.get_memory(int(first.memory[3:])).record.value_text,
                         "Nick")

        existing = self.store.create_memory(
            "relationship", "Gordon trusts Nick", predicate="trusted_person",
            value_text="Nick",
            links=(NewMemoryLink(self.gordon.id, "Subject"),
                   NewMemoryLink(self.nick.id, "Owner")),
            payloads=(NewMemoryPayload("text", "text/plain", "Gordon trusts Nick"),),
        )
        duplicate = self.admit(
            proposal(kind="relationship", predicate="TRUSTED_PERSON", value="Nick",
                     evidence="Gordon trusts Nick", related_entity="Nick",
                     related_role="owner"),
            "Gordon trusts Nick",
        )
        self.assertEqual((duplicate.admission, duplicate.memory),
                         ("duplicate", existing.record.identity))
        self.assertEqual(len(self.store.list_memories_for_entity(self.gordon.id)), 2)

    def test_conflict_dominates_duplicate_in_inconsistent_existing_data(self):
        for value in ("vi", "emacs"):
            self.store.create_memory(
                "fact", f"Gordon prefers {value}", predicate="PREFERRED_EDITOR",
                value_text=value, links=(NewMemoryLink(self.gordon.id, "subject"),),
                payloads=(NewMemoryPayload("text", "text/plain", value),),
            )
        result = self.admit(
            proposal(predicate="preferred_editor", value="vi",
                     evidence="Gordon prefers vi"), "Gordon prefers vi"
        )
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.conflicts, ("MEM2",))


class FailingCreateStore(SQLiteMemoryStore):
    def __init__(self, path):
        super().__init__(path)
        self.create_attempts = 0

    def create_memory(self, *args, **kwargs):
        self.create_attempts += 1
        raise RuntimeError("simulated admission backend failure")


class AdmissionBackend(TextCognitionBackend):
    identifier = "admission-script"

    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.requests = []
        self.results = []
        self.episode_ids = []
        self.refreshed = []

    async def respond(self, message, *, instructions=None, tools=(), tool_executor=None,
                      **kwargs):
        refreshed_instructions = kwargs.get("refreshed_instructions")
        self.requests.append((instructions, tuple(tool.name for tool in tools)))
        if self.sequence:
            self.episode_ids.append(self.app.episode_coordinator.current.id)
            name = self.sequence.pop(0)
            args = ({"query": "Gordon"} if name == "recall_memory"
                    else asdict(proposal()))
            self.results.append(await tool_executor(CognitionToolCall(name, json.dumps(args))))
            if refreshed_instructions is not None:
                self.refreshed.append(refreshed_instructions())
            return "provisional"
        return "I will remember that."


class MemoryAdmissionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, store, sequence):
        backend = AdmissionBackend(sequence)
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(), ApplicationOptions(),
            platform_provider=Platform(), cognition_backend=backend,
            persistent_memory_store=store,
        )
        backend.app = app
        return app, backend

    async def test_operator_remember_is_terminating_semantic_effect(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        store.create_entity("person", "Nick"); gordon = store.create_entity("object", "Gordon")
        app, backend = self.make_app(store, ["remember"])
        await app.start()
        with self.assertLogs("embodied_runtime.app", level="INFO") as captured:
            await app.handle_operator_utterance(
                "Please remember that Gordon's favorite snack is herring.", source="voice"
            )
        logs = "\n".join(captured.output)
        self.assertIn("[MEMORY] episode=E1 admission status=requested", logs)
        self.assertIn("admission result=created episode=E1 status=applied", logs)
        self.assertNotIn("favorite snack", logs)
        self.assertNotIn("herring", logs)
        self.assertEqual(len(backend.requests), 1)
        self.assertIn("remember", backend.requests[0][1])
        self.assertNotIn("remember", app._acquisition_tool_names())
        memories = store.list_memories_for_entity(gordon.id)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].record.source_label, "voice")
        self.assertEqual(len(app.working_memory.snapshot()), 1)
        self.assertEqual(app.episode_coordinator.last.id, 1)
        await app.stop()

    async def test_application_uses_profile_name_for_runtime_self_admission(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        runtime_self = store.create_entity("robot", "Test")
        app, _ = self.make_app(store, [])
        await app.start()
        arguments = asdict(proposal(
            subject="your", predicate="indicator_color", value="blue",
            evidence="your indicator color is blue",
        ))
        result = app._execute_memory_admission(
            CognitionToolCall("remember", json.dumps(arguments)),
            "Remember that your indicator color is blue.", "console",
        )
        outcome = json.loads(result.output)
        self.assertEqual((outcome["status"], outcome["admission"], outcome["entity"]),
                         ("applied", "created", runtime_self.identity))
        await app.stop()

    async def test_episode_trigger_source_is_durable_provenance(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        gordon = store.create_entity("object", "Gordon")
        app, _ = self.make_app(store, ["remember"])
        await app.start()
        await app.request_cognition(
            "Gordon's favorite snack is herring.", source="console"
        )
        self.assertEqual(store.list_memories_for_entity(gordon.id)[0].record.source_label,
                         "console")
        await app.stop()

    async def test_model_authored_summary_argument_is_rejected(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        gordon = store.create_entity("object", "Gordon")
        app, _ = self.make_app(store, [])
        await app.start()
        arguments = asdict(proposal())
        arguments["summary"] = "Gordon likes herring and is allergic to tuna."
        result = app._execute_memory_admission(
            CognitionToolCall("remember", json.dumps(arguments)),
            "Gordon's favorite snack is herring.", "voice",
        )
        self.assertEqual(json.loads(result.output)["status"], "rejected")
        self.assertEqual(json.loads(result.output)["reason"], "invalid_tool_arguments")
        self.assertEqual(store.list_memories_for_entity(gordon.id), ())
        await app.stop()

    async def test_tool_arguments_require_nullable_relationship_keys(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        gordon = store.create_entity("object", "Gordon")
        app, _ = self.make_app(store, [])
        await app.start()
        arguments = asdict(proposal())
        self.assertIsNone(arguments["related_entity"])
        self.assertIsNone(arguments["related_role"])
        accepted = app._execute_memory_admission(
            CognitionToolCall("remember", json.dumps(arguments)),
            "Gordon's favorite snack is herring.", "voice",
        )
        self.assertEqual(json.loads(accepted.output)["status"], "applied")

        arguments = asdict(proposal(predicate="favorite_color", value="blue",
                                    evidence="Gordon's favorite color is blue"))
        arguments.pop("related_role")
        rejected = app._execute_memory_admission(
            CognitionToolCall("remember", json.dumps(arguments)),
            "Gordon's favorite color is blue.", "voice",
        )
        self.assertEqual(json.loads(rejected.output)["status"], "rejected")
        self.assertEqual(json.loads(rejected.output)["reason"], "invalid_tool_arguments")
        self.assertEqual(len(store.list_memories_for_entity(gordon.id)), 1)
        await app.stop()

    async def test_store_failure_is_rejected_and_terminates_without_retry(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = FailingCreateStore(Path(temporary.name) / "memory.sqlite3")
        gordon = store.create_entity("object", "Gordon")
        app, backend = self.make_app(store, ["remember", "recall_memory"])
        await app.start()
        with self.assertLogs("embodied_runtime.app", level="INFO") as captured:
            response = await app.request_cognition(
                "Gordon's favorite snack is herring.", source="console"
            )
        self.assertEqual(response, "provisional")
        self.assertEqual(store.create_attempts, 1)
        self.assertEqual(json.loads(backend.results[0].output), {
            "error": "persistent memory backend failure",
            "reason": "backend_failure", "status": "rejected",
        })
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(backend.sequence, ["recall_memory"])
        self.assertEqual(store.list_memories_for_entity(gordon.id), ())
        self.assertEqual(len(app.working_memory.snapshot()), 1)
        self.assertEqual(app.episode_coordinator.last.id, 1)
        self.assertTrue(any("admission result=rejected reason=backend" in line
                            for line in captured.output))
        await app.stop()

    async def test_refreshed_instructions_ground_final_memory_acknowledgement(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        store.create_entity("object", "Gordon")
        app, backend = self.make_app(store, ["remember"])
        await app.start()
        await app.request_cognition("Gordon's favorite snack is herring.")
        policy = backend.refreshed[0]
        for marker in (
            "admission=created", "admission=duplicate", "status=rejected",
            "remember was not called", "never claim durable memory changed",
        ):
            self.assertIn(marker, policy)
        await app.stop()

    async def test_validation_rejection_is_a_terminating_non_acquisition_effect(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        store.create_entity("object", "Gordon")
        app, backend = self.make_app(store, ["remember", "inspect_self"])
        # Make the scripted remember invalid without changing the backend grammar.
        store.find_entities_exact = lambda name: ()
        await app.start(); await app.request_cognition(
            "Gordon's favorite snack is herring."
        )
        self.assertEqual(json.loads(backend.results[0].output)["status"], "rejected")
        self.assertNotIn("remember", app._acquisition_tool_names())
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(backend.sequence, ["inspect_self"])
        self.assertEqual(len(app.working_memory.snapshot()), 1)
        await app.stop()

    async def test_recall_then_admit_same_episode_and_restart_durability(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "memory.sqlite3"
        store_a = SQLiteMemoryStore(path)
        store_a.create_entity("person", "Nick"); gordon = store_a.create_entity("object", "Gordon")
        app_a, backend = self.make_app(store_a, ["recall_memory", "remember"])
        await app_a.start()
        await app_a.request_cognition("Remember that Gordon's favorite snack is herring.")
        memory_id = store_a.list_memories_for_entity(gordon.id)[0].record.id
        self.assertEqual(backend.episode_ids, [1, 1])
        self.assertEqual(len(backend.requests), 2)
        self.assertIn("acquisitions_used: 1", backend.requests[1][0])
        self.assertEqual(len(app_a.working_memory.snapshot()), 1)
        await app_a.stop()
        store_b = SQLiteMemoryStore(path)
        recalled = MemoryRecallProjector(store_b).recall("Gordon")
        self.assertEqual(recalled.entities[0].id, gordon.id)
        self.assertEqual(recalled.entities[0].memories[0].id, memory_id)
        self.assertIn("herring", recalled.render())
        store_b.close()

    async def test_no_automatic_admission_and_unconfigured_exclusion(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        gordon = store.create_entity("object", "Gordon")
        app, backend = self.make_app(store, [])
        await app.start(); await app.request_cognition("Gordon likes herring")
        self.assertEqual(store.list_memories_for_entity(gordon.id), ())
        await app.stop()
        plain_backend = AdmissionBackend([])
        plain = RobotApplication(RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=Platform(), cognition_backend=plain_backend)
        plain_backend.app = plain
        await plain.start(); await plain.request_cognition("hello")
        self.assertNotIn("remember", plain_backend.requests[0][1])
        self.assertNotIn("remember", plain_backend.requests[0][0])
        await plain.stop()

    async def test_autonomous_projections_exclude_remember(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store = SQLiteMemoryStore(Path(temporary.name) / "memory.sqlite3")
        app, _ = self.make_app(store, [])
        app.options = ApplicationOptions(initiative_enabled=True)
        await app.start(); app.set_goal("test exclusions")
        self.assertNotIn("remember", [tool.name for tool in app.acquisition_tools()])
        self.assertNotIn("remember", [tool.name for tool in app.effect_tools()])
        self.assertNotIn("remember", [tool.name for tool in app.initiative_tools()])
        await app.stop()


if __name__ == "__main__":
    unittest.main()
