import asyncio
import io
import sys
from types import ModuleType
import unittest
from unittest.mock import AsyncMock, patch
import wave

from embodied_runtime.console import RuntimeConsole
from embodied_runtime.voice import (
    FusionHatVoiceProvider, VoiceInteraction, VoiceSessionPolicy,
)


class FakeVoiceProvider:
    def __init__(self, results=()):
        self.results = list(results)
        self.spoken = []
        self.stop_calls = 0
        self.close_calls = 0
        self.cue_calls = 0
        self.listening = asyncio.Event()
        self.release = asyncio.Event()

    async def listen(self):
        self.listening.set()
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        await self.release.wait()
        return None

    async def stop_listening(self):
        self.stop_calls += 1
        self.release.set()

    async def speak(self, text):
        self.spoken.append(text)

    async def play_engagement_cue(self):
        self.cue_calls += 1

    async def close(self):
        self.close_calls += 1


class CoordinatedVoiceProvider:
    """Queue-driven provider that detects overlapping microphone ownership."""

    def __init__(self):
        self.results = asyncio.Queue()
        self.listen_calls = 0
        self.active_listeners = 0
        self.max_active_listeners = 0
        self.spoken = []
        self.cue_calls = 0
        self.timeline = []

    async def listen(self):
        self.timeline.append("listen")
        self.listen_calls += 1
        self.active_listeners += 1
        self.max_active_listeners = max(self.max_active_listeners, self.active_listeners)
        try:
            return await self.results.get()
        finally:
            self.active_listeners -= 1

    async def stop_listening(self):
        if self.active_listeners:
            await self.results.put(None)

    async def speak(self, text):
        self.spoken.append(text)

    async def play_engagement_cue(self):
        if self.active_listeners:
            raise AssertionError("cue overlapped microphone listening")
        self.cue_calls += 1
        self.timeline.append("cue")

    async def close(self):
        pass

    async def feed(self, text):
        await self.results.put(text)

    async def wait_for_listens(self, count):
        for _ in range(100):
            if self.listen_calls >= count:
                return
            await asyncio.sleep(0.001)
        raise AssertionError(f"expected {count} listens, got {self.listen_calls}")


class VoiceInteractionTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, provider, cognition):
        return VoiceInteraction(
            provider, cognition,
            VoiceSessionPolicy(initial_timeout_seconds=0.01,
                               followup_timeout_seconds=0.01),
        )

    async def test_two_turns_use_same_handler_speak_and_close(self):
        provider = FakeVoiceProvider(["first", "follow up"])
        cognition = AsyncMock(side_effect=["one", "two"])
        voice = self.interaction(provider, cognition)
        self.assertEqual(await voice.start(source="console"), "Voice session closed.")
        self.assertEqual(cognition.await_args_list[0].args, ("first",))
        self.assertEqual(cognition.await_args_list[1].args, ("follow up",))
        self.assertEqual(provider.spoken, ["one", "two"])
        self.assertFalse(voice.active)
        self.assertEqual(provider.close_calls, 1)

    async def test_followup_timeout_does_not_call_cognition_again(self):
        provider = FakeVoiceProvider(["first"])
        cognition = AsyncMock(return_value="answer")
        await self.interaction(provider, cognition).start()
        cognition.assert_awaited_once_with("first")
        self.assertEqual(provider.spoken, ["answer"])

    async def test_initial_timeout_does_not_invoke_cognition(self):
        provider = FakeVoiceProvider()
        cognition = AsyncMock()
        await self.interaction(provider, cognition).start()
        cognition.assert_not_awaited()
        self.assertGreaterEqual(provider.stop_calls, 1)

    async def test_only_one_session_can_be_active(self):
        provider = FakeVoiceProvider()
        voice = VoiceInteraction(provider, AsyncMock(), VoiceSessionPolicy(1, 1))
        first = asyncio.create_task(voice.start())
        await provider.listening.wait()
        self.assertEqual(await voice.start(), "Voice interaction already active.")
        await voice.stop()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(voice.active)

    async def test_shutdown_cancels_active_listen_and_cleans_up(self):
        provider = FakeVoiceProvider()
        voice = VoiceInteraction(provider, AsyncMock(), VoiceSessionPolicy(1, 1))
        running = asyncio.create_task(voice.start())
        await provider.listening.wait()
        await voice.stop()
        self.assertTrue(running.cancelled())
        self.assertGreaterEqual(provider.stop_calls, 1)
        self.assertEqual(provider.close_calls, 1)

    async def test_stt_failure_closes_cleanly(self):
        provider = FakeVoiceProvider([RuntimeError("recognizer unavailable")])
        result = await self.interaction(provider, AsyncMock()).start()
        self.assertIn("recognizer unavailable", result)
        self.assertEqual(provider.close_calls, 1)

    async def test_tts_failure_closes_and_cleans_up(self):
        provider = FakeVoiceProvider(["hello"])
        provider.speak = AsyncMock(side_effect=RuntimeError("speaker failed"))
        result = await self.interaction(provider, AsyncMock(return_value="answer")).start()
        self.assertIn("speaker failed", result)
        self.assertEqual(provider.close_calls, 1)

    async def test_unavailable_is_clean(self):
        cognition = AsyncMock()
        result = await VoiceInteraction(None, cognition).start(source="console")
        self.assertEqual(result, "Voice interaction unavailable.")
        cognition.assert_not_awaited()

    async def test_console_voice_is_explicit_trigger(self):
        app = type("App", (), {})()
        app.voice = type("Voice", (), {"start": AsyncMock(return_value="closed")})()
        console = RuntimeConsole(app)
        self.assertEqual(await console.execute_async("voice"), ("closed", False))
        app.voice.start.assert_awaited_once_with(source="console")

    async def test_stop_failure_still_attempts_provider_close(self):
        provider = FakeVoiceProvider(["hello", None])
        provider.stop_listening = AsyncMock(side_effect=RuntimeError("stop failed"))
        voice = self.interaction(provider, AsyncMock(return_value="answer"))
        self.assertEqual(await voice.start(), "Voice session closed.")
        self.assertEqual(provider.close_calls, 1)

    async def test_typed_and_spoken_use_shared_application_seam(self):
        app = type("App", (), {})()
        app.profile = type("Profile", (), {"name": "Test"})()
        app.handle_operator_utterance = AsyncMock(return_value="answer")
        app.voice = VoiceInteraction(
            FakeVoiceProvider(["spoken", None]),
            app.handle_operator_utterance,
            VoiceSessionPolicy(0.01, 0.01),
        )
        console = RuntimeConsole(app)

        self.assertEqual(
            await console.execute_async("ask typed"), ("Test: answer", False)
        )
        await console.execute_async("voice")

        self.assertEqual(
            [call.args for call in app.handle_operator_utterance.await_args_list],
            [("typed",), ("spoken",)],
        )

    async def test_transcript_remains_in_voice_log(self):
        voice = self.interaction(
            FakeVoiceProvider(["recognized words", None]),
            AsyncMock(return_value="answer"),
        )
        with self.assertLogs("embodied_runtime.voice", level="INFO") as logs:
            await voice.start()
        self.assertTrue(any(
            "[VOICE] heard turn=1 text='recognized words'" in entry
            for entry in logs.output
        ))

    async def test_response_subtitle_exactly_matches_tts_text(self):
        provider = FakeVoiceProvider(["recognized words", None])
        voice = self.interaction(provider, AsyncMock(return_value="answer\nline"))
        with self.assertLogs("embodied_runtime.voice", level="INFO") as logs:
            await voice.start()
        self.assertEqual(provider.spoken, ["answer\nline"])
        self.assertTrue(any(
            "[VOICE] response turn=1 text='answer\\nline'" in entry
            for entry in logs.output
        ))

    async def test_local_wake_filters_ambient_and_resumes_after_session(self):
        provider = CoordinatedVoiceProvider()
        cognition = AsyncMock(return_value="answer")
        voice = VoiceInteraction(
            provider, cognition, VoiceSessionPolicy(0.05, 0.01),
            wake_words=["mira", "mirror"],
        )
        voice.start_wake_listener()
        await provider.wait_for_listens(1)
        await provider.feed("background speech")
        await provider.wait_for_listens(2)
        cognition.assert_not_awaited()
        with self.assertLogs("embodied_runtime.voice", level="INFO") as logs:
            await provider.feed("mirror")
            await provider.wait_for_listens(3)
            await provider.feed("question")
            await provider.wait_for_listens(4)
            # Let the bounded follow-up time out and the local wake capture resume.
            await provider.wait_for_listens(5)
        self.assertTrue(any(
            "[VOICE] session_started source=wake_word" in entry
            for entry in logs.output
        ))
        self.assertTrue(any(
            "[VOICE] wake_detected heard='mirror'" in entry for entry in logs.output
        ))
        self.assertEqual(cognition.await_args_list[0].args, ("question",))
        self.assertEqual(provider.spoken, ["answer"])
        self.assertEqual(provider.cue_calls, 1)
        self.assertLess(provider.timeline.index("cue"), provider.timeline.index("listen", 2))
        self.assertEqual(provider.max_active_listeners, 1)
        await voice.stop()
        self.assertFalse(voice.wake_active)

    async def test_wake_matching_is_case_insensitive_and_trimmed(self):
        provider = CoordinatedVoiceProvider()
        cognition = AsyncMock(return_value="answer")
        voice = VoiceInteraction(
            provider, cognition, VoiceSessionPolicy(0.05, 0.01),
            wake_words=["mira", "mirror"],
        )
        voice.start_wake_listener()
        await provider.wait_for_listens(1)
        await provider.feed(" MIRA ")
        await provider.wait_for_listens(2)
        await provider.feed("question")
        await provider.wait_for_listens(3)
        await provider.wait_for_listens(4)
        self.assertEqual(cognition.await_args.args, ("question",))
        await voice.stop()

    async def test_rejected_wake_logs_only_non_empty_text_and_never_calls_cognition(self):
        provider = CoordinatedVoiceProvider()
        cognition = AsyncMock()
        voice = VoiceInteraction(
            provider, cognition, wake_words=["mira", "mirror"]
        )
        voice.start_wake_listener()
        await provider.wait_for_listens(1)

        with self.assertLogs("embodied_runtime.voice", level="INFO") as logs:
            results = ("hi mirror", "unrelated speech", "   ", None)
            for listen_count, result in enumerate(results, start=2):
                await provider.feed(result)
                await provider.wait_for_listens(listen_count)

        rejected = [entry for entry in logs.output if "wake_rejected" in entry]
        self.assertEqual(
            rejected,
            [
                "INFO:embodied_runtime.voice:[VOICE] wake_rejected text='hi mirror'",
                "INFO:embodied_runtime.voice:"
                "[VOICE] wake_rejected text='unrelated speech'",
            ],
        )
        cognition.assert_not_awaited()
        self.assertEqual(provider.cue_calls, 0)
        await voice.stop()

    async def test_wake_resumes_after_voice_session_failure(self):
        provider = CoordinatedVoiceProvider()
        voice = VoiceInteraction(
            provider, AsyncMock(side_effect=RuntimeError("failed")),
            VoiceSessionPolicy(0.05, 0.01), wake_words=["mira", "mirror"],
        )
        voice.start_wake_listener()
        await provider.wait_for_listens(1)
        await provider.feed("mira")
        await provider.wait_for_listens(2)
        await provider.feed("question")
        await provider.wait_for_listens(3)
        self.assertEqual(provider.max_active_listeners, 1)
        self.assertEqual(provider.cue_calls, 1)
        await voice.stop()

    async def test_wake_cue_failure_still_starts_session(self):
        provider = CoordinatedVoiceProvider()
        provider.play_engagement_cue = AsyncMock(side_effect=RuntimeError("no audio"))
        cognition = AsyncMock(return_value="answer")
        voice = VoiceInteraction(
            provider, cognition, VoiceSessionPolicy(0.05, 0.01),
            wake_words=["mira"],
        )
        voice.start_wake_listener()
        await provider.wait_for_listens(1)
        with self.assertLogs("embodied_runtime.voice", level="INFO") as logs:
            await provider.feed("mira")
            await provider.wait_for_listens(2)
            await provider.feed("question")
            await provider.wait_for_listens(4)
        provider.play_engagement_cue.assert_awaited_once_with()
        self.assertEqual(cognition.await_args.args, ("question",))
        self.assertTrue(any(
            "engagement_cue status=failed error=RuntimeError" in entry
            for entry in logs.output
        ))
        await voice.stop()

    async def test_manual_session_suspends_and_resumes_wake_listener(self):
        provider = CoordinatedVoiceProvider()
        voice = VoiceInteraction(
            provider, AsyncMock(return_value="answer"),
            VoiceSessionPolicy(0.05, 0.01), wake_words=["mira", "mirror"],
        )
        voice.start_wake_listener()
        await provider.wait_for_listens(1)
        manual = asyncio.create_task(voice.start(source="console"))
        await provider.wait_for_listens(2)
        await provider.feed("manual question")
        await provider.wait_for_listens(3)
        await manual
        await provider.wait_for_listens(4)
        self.assertEqual(provider.max_active_listeners, 1)
        self.assertEqual(provider.cue_calls, 0)
        await voice.stop()

    async def test_shutdown_cooperatively_stops_wake_capture(self):
        provider = CoordinatedVoiceProvider()
        voice = VoiceInteraction(
            provider, AsyncMock(), wake_words=["mira", "mirror"]
        )
        voice.start_wake_listener()
        await provider.wait_for_listens(1)
        await asyncio.wait_for(voice.stop(), 0.1)
        self.assertFalse(voice.wake_active)
        self.assertEqual(provider.active_listeners, 0)

    async def test_wake_shutdown_stop_failure_still_joins_all_tasks(self):
        provider = CoordinatedVoiceProvider()
        voice = VoiceInteraction(
            provider, AsyncMock(return_value="answer"),
            VoiceSessionPolicy(1, 1), wake_words=["mira", "mirror"],
        )
        voice.start_wake_listener()
        await provider.wait_for_listens(1)
        session = asyncio.create_task(voice.start(source="console"))
        await provider.wait_for_listens(2)
        provider.stop_listening = AsyncMock(side_effect=RuntimeError("stop failed"))

        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            await voice.stop()
        with self.assertRaises(asyncio.CancelledError):
            await session

        self.assertFalse(voice.wake_active)
        self.assertFalse(voice.active)
        self.assertIsNone(voice._wake_task)
        self.assertEqual(provider.active_listeners, 0)

    async def test_timeout_stop_failure_still_joins_listen_task(self):
        provider = CoordinatedVoiceProvider()
        provider.stop_listening = AsyncMock(side_effect=RuntimeError("stop failed"))
        voice = VoiceInteraction(
            provider, AsyncMock(), VoiceSessionPolicy(0.01, 0.01)
        )

        result = await voice.start()

        self.assertIn("stop failed", result)
        self.assertEqual(provider.active_listeners, 0)
        self.assertIsNone(voice._listen_task)

    async def test_failed_manual_wake_handoff_remains_recoverable(self):
        provider = CoordinatedVoiceProvider()
        original_stop = provider.stop_listening
        stop_calls = 0

        async def fail_once():
            nonlocal stop_calls
            stop_calls += 1
            if stop_calls == 1:
                raise RuntimeError("handoff failed")
            await original_stop()

        provider.stop_listening = fail_once
        voice = VoiceInteraction(
            provider, AsyncMock(return_value="answer"),
            VoiceSessionPolicy(0.05, 0.01), wake_words=["mira", "mirror"],
        )
        voice.start_wake_listener()
        await provider.wait_for_listens(1)

        with self.assertRaisesRegex(RuntimeError, "handoff failed"):
            await voice.start(source="console")
        self.assertFalse(voice._session_pending)
        self.assertTrue(voice._wake_enabled.is_set())
        self.assertTrue(voice.wake_active)

        recovered = asyncio.create_task(voice.start(source="console"))
        await provider.wait_for_listens(2)
        await provider.feed("question")
        await recovered
        await provider.wait_for_listens(4)
        self.assertTrue(voice.wake_active)
        await voice.stop()

    async def test_fusion_provider_reenables_speaker_across_sessions(self):
        calls = []
        transcripts = iter(("first", None, "second", None))

        class Vosk:
            def __init__(self, *, language):
                calls.append(("vosk", language))

            def listen(self):
                return next(transcripts)

            def stop_listening(self):
                calls.append(("stop",))

        class Espeak:
            def __init__(self):
                calls.append(("espeak",))

            def say(self, text):
                calls.append(("say", text))

        fusion_hat = ModuleType("fusion_hat")
        stt = ModuleType("fusion_hat.stt")
        stt.Vosk = Vosk
        tts = ModuleType("fusion_hat.tts")
        tts.Espeak = Espeak
        device = ModuleType("fusion_hat.device")
        device.enable_speaker = lambda: calls.append(("enable",))
        device.disable_speaker = lambda: calls.append(("disable",))
        modules = {
            "fusion_hat": fusion_hat,
            "fusion_hat.stt": stt,
            "fusion_hat.tts": tts,
            "fusion_hat.device": device,
        }
        provider = FusionHatVoiceProvider()
        voice = VoiceInteraction(provider, AsyncMock(return_value="answer"))

        with patch.dict(sys.modules, modules):
            await voice.start()
            await voice.start()

        playback_calls = [
            call for call in calls if call[0] in {"enable", "say", "disable"}
        ]
        self.assertEqual(playback_calls, [
            ("enable",), ("say", "answer"), ("disable",),
            ("enable",), ("say", "answer"), ("disable",),
        ])
        self.assertEqual(calls.count(("espeak",)), 1)

    async def test_fusion_engagement_cue_is_fixed_short_wav_and_disables_speaker(self):
        calls = []
        fusion_hat = ModuleType("fusion_hat")
        device = ModuleType("fusion_hat.device")
        device.enable_speaker = lambda: calls.append(("enable",))
        device.disable_speaker = lambda: calls.append(("disable",))
        modules = {"fusion_hat": fusion_hat, "fusion_hat.device": device}
        provider = FusionHatVoiceProvider()

        with patch.dict(sys.modules, modules), patch(
            "embodied_runtime.voice.subprocess.run"
        ) as run:
            await provider.play_engagement_cue()

        self.assertEqual(calls, [("enable",), ("disable",)])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["aplay", "--quiet"])
        self.assertTrue(run.call_args.kwargs["check"])
        with wave.open(io.BytesIO(run.call_args.kwargs["input"]), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth()), (1, 2))
            self.assertEqual(wav.getframerate(), 16_000)
            self.assertEqual(wav.getnframes(), 3_520)
