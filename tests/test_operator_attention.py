import asyncio
import json
import unittest
from datetime import UTC, datetime

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.attention import AttentionEpisodeCoordinator
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.console import RuntimeConsole
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.interaction import (
    CONSOLE_DIALOGUE, VOICE_DIALOGUE, InteractionChannel, InteractionContext,
    InteractionInitiator, InteractionMode, render_dialogue_policy,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.observations import SemanticObservation
from embodied_runtime.voice import VoiceSessionPolicy
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


class MutableClock:
    def __init__(self, now=100):
        self.now = now

    def __call__(self):
        return self.now


class ContinuityBackend(TextCognitionBackend):
    identifier = "continuity"

    def __init__(self, app=None):
        self.app = app
        self.requests = []
        self.current_episode_ids = []
        self.started = asyncio.Event()
        self.mode = "success"

    async def respond(self, message, *, instructions=None, **kwargs):
        self.requests.append((message, instructions))
        self.current_episode_ids.append(self.app.episode_coordinator.current.id)
        self.started.set()
        if self.mode == "failure":
            raise RuntimeError("provider failed")
        if self.mode == "block":
            await asyncio.Event().wait()
        return "E1 semantic response"


class AdvancingAcquisitionBackend(TextCognitionBackend):
    identifier = "advancing-acquisition"

    def __init__(self, clock):
        self.clock = clock
        self.requests = []
        self.refreshed = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((instructions, tuple(tool.name for tool in tools)))
        if len(self.requests) == 1:
            await tool_executor(CognitionToolCall(
                "inspect_self", '{"area": "runtime"}'
            ))
            self.refreshed.append(refreshed_instructions())
            self.clock.now = 165
            return "acquired"
        return "final"


class ScriptedBackend(TextCognitionBackend):
    identifier = "scripted-operator-attention"

    def __init__(self, calls=()):
        self.calls = list(calls)
        self.requests = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tuple(tool.name for tool in tools)))
        if self.calls:
            name, arguments = self.calls.pop(0)
            await tool_executor(CognitionToolCall(name, json.dumps(arguments)))
            return "provisional"
        return "final answer"


class BlockingBackend(TextCognitionBackend):
    identifier = "blocking-operator-attention"

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.maximum = 0
        self.requests = []
        self.cancelled = asyncio.Event()
        self.completed = 0

    async def respond(self, message, **kwargs):
        self.requests.append(message)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.active -= 1
        self.completed += 1
        return message


class CancellationResistantBackend(TextCognitionBackend):
    identifier = "cancellation-resistant"

    def __init__(self):
        self.app = None
        self.entered = asyncio.Event()
        self.attempted_tool = asyncio.Event()
        self.lifecycle_at_attempt = None
        self.completed = False

    async def respond(self, message, *, tool_executor=None, **kwargs):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        self.lifecycle_at_attempt = self.app.state
        self.attempted_tool.set()
        await tool_executor(CognitionToolCall(
            "orient_body", '{"yaw_degrees": 30, "pitch_degrees": 10}'
        ))
        self.completed = True
        return "must not complete"


class VoiceBackend(TextCognitionBackend):
    identifier = "voice-operator-attention"

    def __init__(self):
        self.app = None
        self.episodes = []
        self.instructions = []

    async def respond(self, message, *, instructions=None, **kwargs):
        episode = self.app.episode_coordinator.current
        self.episodes.append((episode.id, episode.trigger_source))
        self.instructions.append(instructions)
        return f"answer {len(self.episodes)}"


class TwoTurnVoice:
    def __init__(self):
        self.results = ["first", "second"]
        self.listen_calls = 0

    async def listen(self):
        self.listen_calls += 1
        return self.results.pop(0)

    async def stop_listening(self):
        pass

    async def play_engagement_cue(self):
        pass

    async def close(self):
        pass


class CheckingTTS:
    def __init__(self):
        self.app = None
        self.spoken = []

    async def speak(self, text):
        if self.app.episode_coordinator.current is not None:
            raise AssertionError("attention episode remained active during TTS")
        self.spoken.append(text)

    async def close(self):
        pass


class OperatorAttentionTests(unittest.IsolatedAsyncioTestCase):
    def app(self, backend, *, initiative=False, voice=None, tts=None, body=None,
            **kwargs):
        return RobotApplication(
            RobotProfile("test", "Test", "test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=initiative),
            platform_provider=Platform(), cognition_backend=backend,
            body_backend=body,
            voice_provider=voice, text_to_speech_provider=tts,
            voice_policy=VoiceSessionPolicy(initial_timeout_seconds=0.1, followup_timeout_seconds=0.1),
            **kwargs,
        )

    async def test_plain_operator_episode_has_shared_identity_and_no_goal_binding(self):
        backend = ScriptedBackend()
        app = self.app(backend)
        await app.start()
        app.set_goal("keep watch")
        self.assertEqual(await app.handle_operator_utterance("hello", source="console"),
                         "final answer")
        episode = app.episode_coordinator.last
        self.assertEqual((episode.id, episode.trigger_kind, episode.trigger_source),
                         (1, "operator_utterance", "console"))
        self.assertIsNone(episode.goal_id)
        self.assertEqual(episode.completion_reason, "handled")
        self.assertIn("id: G1", backend.requests[0][1])
        self.assertEqual(len(app.working_memory.snapshot()), 1)
        await app.stop()

    async def test_console_context_survives_through_operator_execution(self):
        backend = ScriptedBackend()
        app = self.app(backend)
        await app.start()
        seen = []
        run_episode = app._run_operator_episode

        async def record(message, selected_backend, episode, interaction):
            seen.append(interaction)
            return await run_episode(
                message, selected_backend, episode, interaction
            )

        app._run_operator_episode = record
        report, stop = await RuntimeConsole(app).execute_async("ask hello")
        self.assertEqual((report, stop), ("Test: final answer", False))
        self.assertEqual(seen, [CONSOLE_DIALOGUE])
        self.assertIs(seen[0], CONSOLE_DIALOGUE)
        self.assertEqual(app.episode_coordinator.last.trigger_source, "console")
        grounding = CONSOLE_DIALOGUE.render()
        self.assertEqual(backend.requests[0][1].count("Interaction context"), 1)
        self.assertIn(grounding, backend.requests[0][1])
        instructions = backend.requests[0][1]
        policy = render_dialogue_policy(CONSOLE_DIALOGUE)
        self.assertEqual(instructions.count("Dialogue policy"), 1)
        self.assertLess(instructions.index("Working memory"),
                        instructions.index(grounding))
        self.assertLess(instructions.index(grounding), instructions.index(policy))
        self.assertLess(instructions.index(policy),
                        instructions.index("Attention episode"))
        self.assertLess(instructions.index("Attention episode"),
                        instructions.index("Operator episode policy"))
        await app.stop()

    async def test_legacy_source_is_not_promoted_to_interaction_grounding(self):
        backend = ScriptedBackend()
        app = self.app(backend)
        await app.start()
        await app.request_cognition("hello", source="console")
        self.assertEqual(app.episode_coordinator.last.trigger_source, "console")
        self.assertNotIn("Interaction context", backend.requests[0][1])
        self.assertNotIn("Dialogue policy", backend.requests[0][1])
        await app.stop()

    async def test_semantically_valid_distinct_context_is_accepted_and_authoritative(self):
        backend = ScriptedBackend()
        app = self.app(backend)
        await app.start()
        interaction = InteractionContext(
            InteractionChannel.CONSOLE, InteractionMode.DIALOGUE,
            InteractionInitiator.OPERATOR, True,
        )
        self.assertIsNot(interaction, CONSOLE_DIALOGUE)
        await app.request_cognition(
            "hello", interaction=interaction, source="voice"
        )
        self.assertIn(interaction.render(), backend.requests[0][1])
        self.assertEqual(app.episode_coordinator.last.trigger_source, "console")
        await app.stop()

    async def test_previous_successful_operator_turn_and_episode_ground_e2(self):
        clock = MutableClock()
        backend = ContinuityBackend()
        app = self.app(backend, monotonic_clock=clock)
        backend.app = app
        await app.start()
        self.assertEqual(await app.request_cognition("E1 semantic request"),
                         "E1 semantic response")
        self.assertEqual(len(app.working_memory.snapshot()), 1)
        clock.now = 138
        await app.request_cognition("second")
        instructions = backend.requests[1][1]
        self.assertIn("Previous operator turn\n  state: available\n  age_s: 38",
                      instructions)
        self.assertIn("Previous completed episode\n  state: available\n  id: E1\n  age_s: 38",
                      instructions)
        self.assertIn('operator: "E1 semantic request"', instructions)
        self.assertIn('assistant: "E1 semantic response"', instructions)
        self.assertNotIn("Previous completed episode\n  state: available\n  id: E2",
                         instructions)
        self.assertEqual(backend.current_episode_ids, [1, 2])
        temporal = instructions.split("\n\nActive goal", 1)[0].split(
            "Temporal situation", 1)[1]
        self.assertNotIn("E1 semantic request", temporal)
        self.assertEqual(app.episode_coordinator.last.id, 2)
        await app.stop()

    async def test_failed_and_cancelled_turns_preserve_success_marker(self):
        clock = MutableClock()
        backend = ContinuityBackend()
        app = self.app(backend, monotonic_clock=clock)
        backend.app = app
        await app.start()
        await app.request_cognition("successful")
        clock.now = 130
        backend.mode = "failure"
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            await app.request_cognition("failed")
        self.assertEqual(app.temporal_situation().last_operator_turn_age_seconds, 30)
        self.assertEqual(app.episode_coordinator.last.completion_reason, "error")
        clock.now = 150
        backend.mode = "block"
        backend.started.clear()
        task = asyncio.create_task(app.request_cognition("cancelled"))
        await backend.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(app.temporal_situation().last_operator_turn_age_seconds, 50)
        self.assertEqual(app.episode_coordinator.last.completion_reason, "cancelled")
        self.assertEqual(len(app.working_memory.snapshot()), 1)
        self.assertEqual(app.working_memory.snapshot()[0].operator_text, "successful")
        await app.stop()

    async def test_temporal_situation_refreshes_within_acquisition_episode(self):
        clock = MutableClock()
        backend = AdvancingAcquisitionBackend(clock)
        app = self.app(backend, monotonic_clock=clock)
        await app.start()
        app.set_goal("monitor charging")
        clock.now = 160
        app.temporal.schedule(120, "check charging voltage", app.active_goal)
        await app.request_cognition(
            "inspect then answer", interaction=CONSOLE_DIALOGUE
        )
        self.assertEqual(len(backend.requests), 2)
        first, second = backend.requests
        for instructions in (first[0], second[0]):
            self.assertIn("id: E1", instructions)
            self.assertIn("Temporal context", instructions)
            self.assertIn("Working memory\n  state: empty", instructions)
        self.assertIn("age_s: 60", first[0])
        self.assertIn("remaining_s: 120", first[0])
        self.assertIn("age_s: 65", second[0])
        self.assertIn("remaining_s: 115", second[0])
        self.assertIn("acquisitions_remaining: 2", first[0])
        self.assertIn("acquisitions_remaining: 1", second[0])
        self.assertEqual(first[1].count("inspect_self"), 1)
        grounding = CONSOLE_DIALOGUE.render()
        policy = render_dialogue_policy(CONSOLE_DIALOGUE)
        self.assertTrue(all(request[0].count(grounding) == 1
                            for request in backend.requests))
        self.assertTrue(all(request[0].count(policy) == 1
                            for request in backend.requests))
        self.assertEqual(len(backend.refreshed), 1)
        self.assertEqual(backend.refreshed[0].count(grounding), 1)
        self.assertEqual(backend.refreshed[0].count(policy), 1)
        self.assertEqual(app.episode_coordinator.last.id, 1)
        self.assertEqual(len(app.working_memory.snapshot()), 1)
        await app.stop()

    async def test_two_acquisitions_use_three_fresh_requests_and_one_memory_turn(self):
        backend = ScriptedBackend((
            ("inspect_self", {"area": "runtime"}),
            ("inspect_self", {"area": "storage"}),
        ))
        app = self.app(backend)
        await app.start()
        self.assertEqual(await app.request_cognition("inspect both"), "final answer")
        self.assertEqual(len(backend.requests), 3)
        self.assertEqual({request[0] for request in backend.requests}, {"inspect both"})
        self.assertIn("acquisitions_used: 1", backend.requests[1][1])
        self.assertIn("acquisitions_used: 2", backend.requests[2][1])
        self.assertNotIn("inspect_self", backend.requests[2][2])
        turn = app.working_memory.snapshot()[0]
        self.assertEqual([outcome.name for outcome in turn.tool_outcomes],
                         ["inspect_self", "inspect_self"])
        await app.stop()

    async def test_temporal_grounding_is_fresh_across_operator_acquisition(self):
        backend = ScriptedBackend((("inspect_self", {"area": "runtime"}),))
        instants = iter((datetime(2026, 9, 10, 22, 0, 1, tzinfo=UTC),
                         datetime(2026, 9, 10, 22, 0, 5, tzinfo=UTC)))
        app = self.app(backend, timezone_name="America/Toronto",
                       wall_clock=lambda: next(instants))
        await app.start()
        await app.request_cognition("what is true now?")
        self.assertEqual(len(backend.requests), 2)
        first, second = (request[1] for request in backend.requests)
        self.assertIn("Temporal context", first)
        self.assertIn("local_time: 18:00:01", first)
        self.assertIn("local_time: 18:00:05", second)
        self.assertIn("id: E1", first)
        self.assertIn("id: E1", second)
        self.assertIn("Working memory\n  state: empty", first)
        self.assertIn("Working memory\n  state: empty", second)
        self.assertEqual(backend.requests[0][2].count("inspect_self"), 1)
        self.assertNotIn("get_time", backend.requests[0][2])
        await app.stop()

    async def test_operator_requests_are_serialized(self):
        backend = BlockingBackend()
        app = self.app(backend)
        await app.start()
        first = asyncio.create_task(app.request_cognition("one"))
        await backend.entered.wait()
        second = asyncio.create_task(app.request_cognition("two"))
        await asyncio.sleep(0)
        self.assertEqual(backend.maximum, 1)
        self.assertEqual(app.episode_coordinator.current.id, 1)
        backend.release.set()
        self.assertEqual(await first, "one")
        self.assertEqual(await second, "two")
        self.assertEqual(backend.maximum, 1)
        self.assertEqual(app.episode_coordinator.last.id, 2)
        await app.stop()

    async def test_autonomous_start_yields_to_operator_waiter(self):
        coordinator = AttentionEpisodeCoordinator()
        autonomous = coordinator.try_start("event", "test", "concern", 1)
        waiter = asyncio.create_task(coordinator.start_operator("voice", "respond"))
        await asyncio.sleep(0)
        coordinator.close(autonomous, "handled")
        self.assertIsNone(coordinator.try_start("event", "test", "concern", 1))
        operator = await waiter
        self.assertEqual((operator.id, operator.trigger_source), (2, "voice"))
        coordinator.close(operator, "handled")

    async def test_operator_active_suppresses_ordinary_autonomous_event(self):
        backend = BlockingBackend()
        app = self.app(backend, initiative=True)
        await app.start(); app.set_goal("goal")
        operator = asyncio.create_task(app.request_cognition("operator"))
        await backend.entered.wait()
        await app.attention._consider(SemanticObservation("test", "test", ()))
        self.assertEqual(len(app.working_memory.snapshot()), 0)
        self.assertEqual(backend.maximum, 1)
        backend.release.set(); await operator; await asyncio.sleep(0)
        self.assertEqual(app.episode_coordinator.last.trigger_kind,
                         "operator_utterance")
        self.assertEqual(backend.maximum, 1)
        await app.stop()

    async def test_autonomous_active_blocks_operator_and_new_events(self):
        backend = BlockingBackend()
        app = self.app(backend, initiative=True)
        await app.start(); app.set_goal("goal")
        await app.attention._consider(SemanticObservation("first", "test", ()))
        await backend.entered.wait()
        operator = asyncio.create_task(app.request_cognition("operator"))
        await asyncio.sleep(0)
        await app.attention._consider(SemanticObservation("second", "test", ()))
        self.assertTrue(app.episode_coordinator.operator_waiting)
        self.assertEqual(app.episode_coordinator.current.id, 1)
        backend.release.set(); await operator
        self.assertEqual(backend.maximum, 1)
        self.assertEqual(app.episode_coordinator.last.id, 2)
        self.assertEqual(app.episode_coordinator.last.trigger_kind,
                         "operator_utterance")
        await app.stop()

    async def test_shutdown_cancels_operator_after_coordinator_wait(self):
        backend = BlockingBackend()
        app = self.app(backend, initiative=True)
        await app.start(); app.set_goal("goal")
        await app.attention._consider(SemanticObservation("first", "test", ()))
        await backend.entered.wait()
        operator = asyncio.create_task(app.request_cognition("operator"))
        await asyncio.sleep(0)
        self.assertTrue(app.episode_coordinator.operator_waiting)
        await app.stop()
        with self.assertRaisesRegex(RuntimeError, "running application"):
            await operator
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(app.working_memory.snapshot(), ())
        self.assertIsNone(app.episode_coordinator.current)
        self.assertFalse(app.episode_coordinator.operator_waiting)

    async def test_shutdown_cancels_and_joins_active_operator_cognition(self):
        backend = BlockingBackend()
        app = self.app(backend)
        await app.start()
        operator = asyncio.create_task(app.request_cognition("operator"))
        await backend.entered.wait()
        episode = app.episode_coordinator.current
        self.assertEqual((episode.id, episode.trigger_kind),
                         (1, "operator_utterance"))
        self.assertIs(app._active_operator_cognition_task, operator)

        await app.stop()

        self.assertTrue(operator.cancelled())
        self.assertTrue(backend.cancelled.is_set())
        self.assertEqual(backend.active, 0)
        self.assertEqual(backend.completed, 0)
        self.assertEqual(app.state.value, "stopped")
        self.assertIsNone(app.episode_coordinator.current)
        self.assertEqual(app.episode_coordinator.last.id, 1)
        self.assertEqual(app.episode_coordinator.last.completion_reason, "cancelled")
        self.assertEqual(app.working_memory.snapshot(), ())
        self.assertIsNone(app._active_operator_cognition_task)

    async def test_shutdown_fences_tool_from_cancellation_resistant_backend(self):
        backend = CancellationResistantBackend()
        body = VirtualBodyBackend()
        app = self.app(backend, body=body)
        backend.app = app
        await app.start()
        before = app.runtime_state.body
        closed = []
        original_close = app.episode_coordinator.close

        def record_close(episode, reason):
            closed.append((episode.id, reason))
            return original_close(episode, reason)

        app.episode_coordinator.close = record_close
        operator = asyncio.create_task(app.request_cognition("move"))
        await backend.entered.wait()

        await app.stop()

        self.assertTrue(backend.attempted_tool.is_set())
        self.assertNotEqual(backend.lifecycle_at_attempt.value, "running")
        self.assertFalse(backend.completed)
        self.assertTrue(operator.cancelled())
        self.assertEqual(app.runtime_state.body, before)
        self.assertEqual(closed, [(1, "cancelled")])
        self.assertEqual(app.episode_coordinator.last.completion_reason, "cancelled")
        self.assertIsNone(app.episode_coordinator.current)
        self.assertEqual(app.working_memory.snapshot(), ())
        self.assertIsNone(app._active_operator_cognition_task)

    async def test_two_voice_turns_are_distinct_episodes_closed_before_tts(self):
        backend, voice, tts = VoiceBackend(), TwoTurnVoice(), CheckingTTS()
        app = self.app(backend, voice=voice, tts=tts)
        backend.app = tts.app = app
        await app.start()
        seen = []
        run_episode = app._run_operator_episode

        async def record(message, selected_backend, episode, interaction):
            seen.append(interaction)
            return await run_episode(
                message, selected_backend, episode, interaction
            )

        app._run_operator_episode = record
        self.assertEqual(await app.voice.start(source="console"),
                         "Voice session closed.")
        self.assertEqual(seen, [VOICE_DIALOGUE, VOICE_DIALOGUE])
        self.assertTrue(all(context is VOICE_DIALOGUE for context in seen))
        self.assertEqual(backend.episodes, [(1, "voice"), (2, "voice")])
        self.assertTrue(all(text.count(VOICE_DIALOGUE.render()) == 1
                            for text in backend.instructions))
        voice_policy = render_dialogue_policy(VOICE_DIALOGUE)
        self.assertTrue(all(text.count(voice_policy) == 1
                            for text in backend.instructions))
        for instructions in backend.instructions:
            self.assertLess(instructions.index("Working memory"),
                            instructions.index(VOICE_DIALOGUE.render()))
            self.assertLess(instructions.index(VOICE_DIALOGUE.render()),
                            instructions.index(voice_policy))
            self.assertLess(instructions.index(voice_policy),
                            instructions.index("Attention episode"))
            self.assertLess(instructions.index("Attention episode"),
                            instructions.index("Operator episode policy"))
        self.assertEqual(tts.spoken, ["answer 1", "answer 2"])
        self.assertEqual(voice.listen_calls, 2)
        self.assertIn("Working memory\n  state: empty", backend.instructions[0])
        self.assertIn('operator: "first"', backend.instructions[1])
        self.assertEqual(len(app.working_memory.snapshot()), 2)
        self.assertIsNone(app.episode_coordinator.current)
        await app.stop()

    async def test_dialogue_channel_does_not_change_operator_tool_authority(self):
        backend = ScriptedBackend()
        app = self.app(backend)
        await app.start()
        await app.request_cognition("console", interaction=CONSOLE_DIALOGUE)
        await app.request_cognition("voice", interaction=VOICE_DIALOGUE)
        self.assertEqual(backend.requests[0][2], backend.requests[1][2])
        await app.stop()

    async def test_console_to_voice_keeps_shared_history_and_changes_current_policy(self):
        backend = ScriptedBackend()
        app = self.app(backend)
        await app.start()
        self.assertEqual(
            await RuntimeConsole(app).execute_async(
                "ask The project codename is Bluebird."
            ),
            ("Test: final answer", False),
        )
        await app.request_cognition("What was the codename?", interaction=VOICE_DIALOGUE)
        voice_instructions = backend.requests[1][1]
        self.assertIn('operator: "The project codename is Bluebird."', voice_instructions)
        self.assertIn('assistant: "final answer"', voice_instructions)
        self.assertIn(VOICE_DIALOGUE.render(), voice_instructions)
        self.assertIn(render_dialogue_policy(VOICE_DIALOGUE), voice_instructions)
        self.assertNotIn(render_dialogue_policy(CONSOLE_DIALOGUE), voice_instructions)
        self.assertEqual([turn.operator_text for turn in app.working_memory.snapshot()], [
            "The project codename is Bluebird.", "What was the codename?",
        ])
        self.assertEqual(app.episode_coordinator.last.id, 2)
        await app.stop()

    async def test_voice_to_console_keeps_shared_history_and_changes_current_policy(self):
        backend = ScriptedBackend()
        app = self.app(backend)
        await app.start()
        await app.request_cognition(
            "Let's call the test object Bluebird.", interaction=VOICE_DIALOGUE
        )
        self.assertEqual(
            await RuntimeConsole(app).execute_async(
                "ask What did I just call the test object?"
            ),
            ("Test: final answer", False),
        )
        console_instructions = backend.requests[1][1]
        self.assertIn('operator: "Let\'s call the test object Bluebird."',
                      console_instructions)
        self.assertIn('assistant: "final answer"', console_instructions)
        self.assertIn(CONSOLE_DIALOGUE.render(), console_instructions)
        self.assertIn(render_dialogue_policy(CONSOLE_DIALOGUE), console_instructions)
        self.assertNotIn(render_dialogue_policy(VOICE_DIALOGUE), console_instructions)
        self.assertEqual(len(app.working_memory.snapshot()), 2)
        await app.stop()


if __name__ == "__main__":
    unittest.main()
