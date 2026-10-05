from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest

from embodied_runtime.app import RobotApplication
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.conversation_history import (
    MAX_ASSISTANT_TEXT_CHARS, MAX_OPERATOR_TEXT_CHARS,
    SQLiteConversationHistoryStore, render_conversation_history,
)
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.interaction import (
    CONSOLE_DIALOGUE, VOICE_DIALOGUE, InteractionChannel, InteractionContext,
    InteractionCadence, InteractionInitiator, InteractionMode,
)
from embodied_runtime.profile import RobotProfile
from tests.test_platform import snapshot
from tests.test_working_memory import ScriptedCognition


REMOTE_DIALOGUE = InteractionContext(
    channel=InteractionChannel.REMOTE_TEXT, mode=InteractionMode.DIALOGUE,
    initiator=InteractionInitiator.OPERATOR, response_expected=True,
    cadence=InteractionCadence.BOUNDED_TURN,
)


class StaticPlatform:
    def snapshot(self):
        return snapshot()


class ConversationHistoryStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "conversation.sqlite3"
        self.store = SQLiteConversationHistoryStore(self.path)
        self.addCleanup(self.store.close)
        self.now = datetime(2026, 10, 5, 12, tzinfo=UTC)

    def test_append_reopen_unicode_bounds_and_schema(self):
        record = self.store.append("old", self.now, InteractionChannel.REMOTE_TEXT,
                                   "🦜" + "x" * 3000, "réponse" + "y" * 3000)
        self.assertEqual(len(record.operator_text), MAX_OPERATOR_TEXT_CHARS)
        self.assertEqual(len(record.assistant_text), MAX_ASSISTANT_TEXT_CHARS)
        self.store.close()
        reopened = SQLiteConversationHistoryStore(self.path)
        self.addCleanup(reopened.close)
        selected = reopened.select_prior("new", InteractionChannel.VOICE)
        self.assertEqual(selected, (record,))
        with sqlite3.connect(self.path) as connection:
            fields = {row[1] for row in connection.execute(
                "PRAGMA table_info(conversation_turns)")}
        self.assertEqual(fields, {"id", "session_id", "completed_at", "channel",
                                  "operator_text", "assistant_text"})

    def test_channel_affinity_session_exclusion_and_chronological_rendering(self):
        channels = [InteractionChannel.VOICE] * 4 + [InteractionChannel.CONSOLE] * 2 + [
            InteractionChannel.REMOTE_TEXT]
        for index, channel in enumerate(channels):
            self.store.append("old", self.now + timedelta(minutes=index), channel,
                              f"operator {index}", f"assistant {index}")
        self.store.append("current", self.now + timedelta(hours=1),
                          InteractionChannel.VOICE, "duplicate", "duplicate")
        selected = self.store.select_prior("current", InteractionChannel.VOICE)
        self.assertEqual(len(selected), 5)
        self.assertEqual(sum(r.channel is InteractionChannel.VOICE for r in selected), 3)
        self.assertEqual([r.completed_at for r in selected],
                         sorted(r.completed_at for r in selected))
        self.assertNotIn("duplicate", render_conversation_history(selected))

    def test_invalid_channel_and_close_fail_safely(self):
        with self.assertRaises(ValueError):
            self.store.append("session", self.now, "voice", "a", "b")
        self.store.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            self.store.select_prior("session", InteractionChannel.VOICE)


class ConversationHistoryApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "conversation.sqlite3"

    def app(self, backend, store, session):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=StaticPlatform(), body_backend=VirtualBodyBackend(),
            cognition_backend=backend, conversation_history_store=store,
            conversation_session_id=session,
        )

    async def test_restart_cross_channel_and_current_session_no_duplication(self):
        first_backend = ScriptedCognition(["Camera plan acknowledged.", "same session"])
        first = self.app(first_backend, SQLiteConversationHistoryStore(self.path), "R1")
        await first.start()
        await first.request_cognition("I'm going to test the camera when I get home.",
                                      interaction=REMOTE_DIALOGUE)
        await first.request_cognition("What did I say?", interaction=VOICE_DIALOGUE)
        current = first_backend.calls[1][1]
        self.assertIn("channel: remote_text", current)
        self.assertNotIn("Prior conversation history", current)
        await first.stop()

        second_backend = ScriptedCognition(["the camera"])
        second = self.app(second_backend, SQLiteConversationHistoryStore(self.path), "R2")
        await second.start()
        await second.request_cognition("What was I going to test?",
                                       interaction=VOICE_DIALOGUE)
        instructions = second_backend.calls[0][1]
        self.assertIn("Prior conversation history", instructions)
        self.assertIn("channel: remote_text", instructions)
        self.assertIn("Interaction context\n  channel: voice", instructions)
        self.assertIn("Dialogue policy\n  medium: spoken", instructions)
        await second.stop()

    async def test_voice_restart_to_remote_and_failure_not_persisted(self):
        failing = ScriptedCognition([RuntimeError("provider")])
        first = self.app(failing, SQLiteConversationHistoryStore(self.path), "R1")
        await first.start()
        with self.assertRaises(RuntimeError):
            await first.request_cognition("not durable", interaction=CONSOLE_DIALOGUE)
        await first.stop()
        seed = SQLiteConversationHistoryStore(self.path)
        seed.append("R1", datetime.now(UTC), InteractionChannel.VOICE,
                    "Bluebird was our codename.", "Understood.")
        seed.close()
        backend = ScriptedCognition(["Bluebird"])
        second = self.app(backend, SQLiteConversationHistoryStore(self.path), "R2")
        await second.start()
        await second.request_cognition("What was the codename?", interaction=REMOTE_DIALOGUE)
        instructions = backend.calls[0][1]
        self.assertIn("channel: voice", instructions)
        self.assertIn("Interaction context\n  channel: remote_text", instructions)
        await second.stop()

    async def test_persistence_failure_is_nonfatal(self):
        store = SQLiteConversationHistoryStore(self.path)
        app = self.app(ScriptedCognition(["success"]), store, "R1")
        await app.start()
        store.close()
        self.assertEqual(await app.request_cognition("hello", interaction=CONSOLE_DIALOGUE),
                         "success")
        self.assertEqual(len(app.working_memory), 1)
        await app.stop()
