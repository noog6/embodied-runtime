import asyncio
import argparse
import io
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import wave

from embodied_runtime.console import RuntimeConsole
from embodied_runtime.cli import build_text_to_speech_provider
from embodied_runtime.voice import (
    FusionHatElevenLabsTTSProvider, FusionHatEspeakTTSProvider,
    FusionHatOpenAITTSProvider,
    FusionHatPiperTTSProvider,
    FusionHatVoiceProvider, VoiceInteraction, VoiceSessionPolicy,
)


class FakeTextToSpeechProvider:
    def __init__(self, voice_provider=None):
        self.spoken = []
        self.close_calls = 0
        self.voice_provider = voice_provider

    async def speak(self, text):
        if self.voice_provider is not None and getattr(
            self.voice_provider, "active_listeners", 0
        ):
            raise AssertionError("TTS overlapped microphone listening")
        self.spoken.append(text)

    async def close(self):
        self.close_calls += 1


class FakeVoiceProvider:
    def __init__(self, results=()):
        self.results = list(results)
        self.stop_calls = 0
        self.close_calls = 0
        self.cue_calls = 0
        self.listening = asyncio.Event()
        self.release = asyncio.Event()
        self.tts = FakeTextToSpeechProvider(self)

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
        self.cue_calls = 0
        self.timeline = []
        self.tts = FakeTextToSpeechProvider(self)

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
            provider, provider.tts, cognition,
            VoiceSessionPolicy(initial_timeout_seconds=0.01,
                               followup_timeout_seconds=0.01),
        )

    def test_tts_selection_is_physical_and_preserves_all_providers(self):
        base = dict(voice_enabled=True, hardware="fusion-hat", piper_model="model",
                    openai_tts_model="model", openai_tts_voice="voice",
                    elevenlabs_tts_model="el-model",
                    elevenlabs_tts_voice_id="el-voice")
        with patch("embodied_runtime.cli.FusionHatEspeakTTSProvider") as espeak, \
             patch("embodied_runtime.cli.FusionHatPiperTTSProvider") as piper, \
             patch("embodied_runtime.cli.FusionHatOpenAITTSProvider") as openai, \
             patch("embodied_runtime.cli.FusionHatElevenLabsTTSProvider") as elevenlabs:
            build_text_to_speech_provider(argparse.Namespace(**base, tts="espeak"))
            espeak.assert_called_once_with()
            build_text_to_speech_provider(argparse.Namespace(**base, tts="piper"))
            piper.assert_called_once_with(model_path="model")
            build_text_to_speech_provider(argparse.Namespace(**base, tts="openai"))
            openai.assert_called_once_with(model="model", voice="voice")
            build_text_to_speech_provider(argparse.Namespace(**base, tts="elevenlabs"))
            elevenlabs.assert_called_once_with(model="el-model", voice_id="el-voice")

            for disabled in (
                {**base, "voice_enabled": False, "tts": "openai"},
                {**base, "hardware": "virtual", "tts": "openai"},
                {**base, "voice_enabled": False, "tts": "elevenlabs"},
                {**base, "hardware": "virtual", "tts": "elevenlabs"},
            ):
                self.assertIsNone(build_text_to_speech_provider(
                    argparse.Namespace(**disabled)
                ))
            self.assertEqual(openai.call_count, 1)
            self.assertEqual(elevenlabs.call_count, 1)

    async def test_two_turns_use_same_handler_speak_and_close(self):
        provider = FakeVoiceProvider(["first", "follow up"])
        cognition = AsyncMock(side_effect=["one", "two"])
        voice = self.interaction(provider, cognition)
        self.assertEqual(await voice.start(source="console"), "Voice session closed.")
        self.assertEqual(cognition.await_args_list[0].args, ("first",))
        self.assertEqual(cognition.await_args_list[1].args, ("follow up",))
        self.assertEqual(provider.tts.spoken, ["one", "two"])
        self.assertFalse(voice.active)
        self.assertEqual(provider.close_calls, 1)
        self.assertEqual(provider.tts.close_calls, 1)

    async def test_followup_timeout_does_not_call_cognition_again(self):
        provider = FakeVoiceProvider(["first"])
        cognition = AsyncMock(return_value="answer")
        await self.interaction(provider, cognition).start()
        cognition.assert_awaited_once_with("first")
        self.assertEqual(provider.tts.spoken, ["answer"])

    async def test_initial_timeout_does_not_invoke_cognition(self):
        provider = FakeVoiceProvider()
        cognition = AsyncMock()
        await self.interaction(provider, cognition).start()
        cognition.assert_not_awaited()
        self.assertGreaterEqual(provider.stop_calls, 1)

    async def test_only_one_session_can_be_active(self):
        provider = FakeVoiceProvider()
        voice = VoiceInteraction(
            provider, provider.tts, AsyncMock(), VoiceSessionPolicy(1, 1)
        )
        first = asyncio.create_task(voice.start())
        await provider.listening.wait()
        self.assertEqual(await voice.start(), "Voice interaction already active.")
        await voice.stop()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(voice.active)

    async def test_shutdown_cancels_active_listen_and_cleans_up(self):
        provider = FakeVoiceProvider()
        voice = VoiceInteraction(
            provider, provider.tts, AsyncMock(), VoiceSessionPolicy(1, 1)
        )
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
        provider.tts.speak = AsyncMock(side_effect=RuntimeError("speaker failed"))
        result = await self.interaction(provider, AsyncMock(return_value="answer")).start()
        self.assertIn("speaker failed", result)
        self.assertEqual(provider.close_calls, 1)

    async def test_unavailable_is_clean(self):
        cognition = AsyncMock()
        result = await VoiceInteraction(None, None, cognition).start(source="console")
        self.assertEqual(result, "Voice interaction unavailable.")
        cognition.assert_not_awaited()

    async def test_input_without_tts_is_unavailable(self):
        provider = FakeVoiceProvider(["hello"])
        cognition = AsyncMock()
        voice = VoiceInteraction(provider, None, cognition)
        self.assertFalse(voice.available)
        self.assertEqual(await voice.start(), "Voice interaction unavailable.")
        cognition.assert_not_awaited()

    async def test_cleanup_failures_do_not_abandon_the_other_provider(self):
        provider = FakeVoiceProvider([None])
        provider.close = AsyncMock(side_effect=RuntimeError("input close failed"))
        with self.assertLogs("embodied_runtime.voice", level="ERROR"):
            await self.interaction(provider, AsyncMock()).start()
        self.assertEqual(provider.tts.close_calls, 1)

        provider = FakeVoiceProvider([None])
        provider.tts.close = AsyncMock(side_effect=RuntimeError("TTS close failed"))
        with self.assertLogs("embodied_runtime.voice", level="ERROR"):
            await self.interaction(provider, AsyncMock()).start()
        self.assertEqual(provider.close_calls, 1)

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
            (voice_provider := FakeVoiceProvider(["spoken", None])),
            voice_provider.tts,
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
        self.assertEqual(provider.tts.spoken, ["answer\nline"])
        self.assertTrue(any(
            "[VOICE] response turn=1 text='answer\\nline'" in entry
            for entry in logs.output
        ))

    async def test_local_wake_filters_ambient_and_resumes_after_session(self):
        provider = CoordinatedVoiceProvider()
        cognition = AsyncMock(return_value="answer")
        voice = VoiceInteraction(
            provider, provider.tts, cognition, VoiceSessionPolicy(0.05, 0.01),
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
        self.assertEqual(provider.tts.spoken, ["answer"])
        self.assertEqual(provider.cue_calls, 1)
        self.assertLess(provider.timeline.index("cue"), provider.timeline.index("listen", 2))
        self.assertEqual(provider.max_active_listeners, 1)
        await voice.stop()
        self.assertFalse(voice.wake_active)

    async def test_wake_matching_is_case_insensitive_and_trimmed(self):
        provider = CoordinatedVoiceProvider()
        cognition = AsyncMock(return_value="answer")
        voice = VoiceInteraction(
            provider, provider.tts, cognition, VoiceSessionPolicy(0.05, 0.01),
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
            provider, provider.tts, cognition, wake_words=["mira", "mirror"]
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
            provider, provider.tts, AsyncMock(side_effect=RuntimeError("failed")),
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
            provider, provider.tts, cognition, VoiceSessionPolicy(0.05, 0.01),
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
            provider, provider.tts, AsyncMock(return_value="answer"),
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
            provider, provider.tts, AsyncMock(), wake_words=["mira", "mirror"]
        )
        voice.start_wake_listener()
        await provider.wait_for_listens(1)
        await asyncio.wait_for(voice.stop(), 0.1)
        self.assertFalse(voice.wake_active)
        self.assertEqual(provider.active_listeners, 0)

    async def test_wake_shutdown_stop_failure_still_joins_all_tasks(self):
        provider = CoordinatedVoiceProvider()
        voice = VoiceInteraction(
            provider, provider.tts, AsyncMock(return_value="answer"),
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
            provider, provider.tts, AsyncMock(), VoiceSessionPolicy(0.01, 0.01)
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
            provider, provider.tts, AsyncMock(return_value="answer"),
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
        tts_provider = FusionHatEspeakTTSProvider()
        voice = VoiceInteraction(
            provider, tts_provider, AsyncMock(return_value="answer")
        )
        self.assertNotIn(("espeak",), calls)

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

    async def test_piper_is_lazy_reused_and_plays_in_memory_wav(self):
        calls = []

        class Voice:
            def synthesize_wav(self, text, wav_writer):
                calls.append(("synthesize", text))
                wav_writer.setnchannels(1)
                wav_writer.setsampwidth(2)
                wav_writer.setframerate(16_000)
                wav_writer.writeframes(b"\0\0")

        class PiperVoice:
            @staticmethod
            def load(path):
                calls.append(("load", path))
                return Voice()

        piper = ModuleType("piper")
        piper.PiperVoice = PiperVoice
        fusion_hat = ModuleType("fusion_hat")
        device = ModuleType("fusion_hat.device")
        device.enable_speaker = lambda: calls.append(("enable",))
        device.disable_speaker = lambda: calls.append(("disable",))
        with tempfile.NamedTemporaryFile(suffix=".onnx") as model:
            with patch.dict(sys.modules, {
                "piper": piper, "fusion_hat": fusion_hat,
                "fusion_hat.device": device,
            }), patch("embodied_runtime.voice.subprocess.run") as run:
                provider = FusionHatPiperTTSProvider(model_path=model.name)
                self.assertEqual(calls, [])
                await provider.speak("Exact **text**")
                await provider.close()
                await provider.speak("again")

        self.assertEqual(calls.count(("load", model.name)), 1)
        self.assertIn(("synthesize", "Exact **text**"), calls)
        self.assertEqual(calls[:3], [
            ("load", model.name), ("synthesize", "Exact **text**"), ("enable",)
        ])
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.args[0], ["aplay", "--quiet"])
            self.assertTrue(call.kwargs["input"].startswith(b"RIFF"))
            self.assertTrue(call.kwargs["check"])

    async def test_piper_synthesis_failure_never_enables_speaker(self):
        calls = []

        class PiperVoice:
            @staticmethod
            def load(path):
                return PiperVoice()

            def synthesize_wav(self, text, wav_writer):
                raise RuntimeError("synthesis failed")

        piper = ModuleType("piper")
        piper.PiperVoice = PiperVoice
        fusion_hat = ModuleType("fusion_hat")
        device = ModuleType("fusion_hat.device")
        device.enable_speaker = lambda: calls.append("enable")
        device.disable_speaker = lambda: calls.append("disable")
        with tempfile.NamedTemporaryFile(suffix=".onnx") as model, patch.dict(
            sys.modules, {"piper": piper, "fusion_hat": fusion_hat,
                          "fusion_hat.device": device}
        ):
            provider = FusionHatPiperTTSProvider(model_path=model.name)
            with self.assertRaisesRegex(RuntimeError, "synthesis failed"):
                await provider.speak("hello")
        self.assertEqual(calls, [])

    async def test_piper_playback_failure_disables_speaker(self):
        calls = []

        class Voice:
            def synthesize_wav(self, text, wav_writer):
                wav_writer.setnchannels(1); wav_writer.setsampwidth(2)
                wav_writer.setframerate(16_000); wav_writer.writeframes(b"\0\0")

        piper = ModuleType("piper")
        piper.PiperVoice = type("PiperVoice", (), {"load": staticmethod(lambda _: Voice())})
        fusion_hat = ModuleType("fusion_hat")
        device = ModuleType("fusion_hat.device")
        device.enable_speaker = lambda: calls.append("enable")
        device.disable_speaker = lambda: calls.append("disable")
        with tempfile.NamedTemporaryFile(suffix=".onnx") as model, patch.dict(
            sys.modules, {"piper": piper, "fusion_hat": fusion_hat,
                          "fusion_hat.device": device}
        ), patch("embodied_runtime.voice.subprocess.run",
                 side_effect=RuntimeError("playback failed")):
            provider = FusionHatPiperTTSProvider(model_path=model.name)
            with self.assertRaisesRegex(RuntimeError, "playback failed"):
                await provider.speak("hello")
        self.assertEqual(calls, ["enable", "disable"])

    def openai_modules(self, calls, create):
        client = SimpleNamespace(audio=SimpleNamespace(
            speech=SimpleNamespace(create=create)
        ))
        openai = ModuleType("openai")
        openai.AsyncOpenAI = lambda: client
        fusion_hat = ModuleType("fusion_hat")
        device = ModuleType("fusion_hat.device")
        device.enable_speaker = lambda: calls.append("enable")
        device.disable_speaker = lambda: calls.append("disable")
        return {"openai": openai, "fusion_hat": fusion_hat,
                "fusion_hat.device": device}

    def elevenlabs_modules(self, calls, convert):
        client = SimpleNamespace(
            text_to_speech=SimpleNamespace(convert=convert)
        )
        elevenlabs = ModuleType("elevenlabs")
        client_module = ModuleType("elevenlabs.client")
        client_module.AsyncElevenLabs = lambda **kwargs: client
        fusion_hat = ModuleType("fusion_hat")
        device = ModuleType("fusion_hat.device")
        device.enable_speaker = lambda: calls.append("enable")
        device.disable_speaker = lambda: calls.append("disable")
        return {"elevenlabs": elevenlabs, "elevenlabs.client": client_module,
                "fusion_hat": fusion_hat, "fusion_hat.device": device}

    @staticmethod
    def wav_bytes(*, frames=16_000, sample_rate=16_000):
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(b"\0\0" * frames)
        return output.getvalue()

    async def test_openai_tts_logs_independent_times_and_wav_duration(self):
        calls = []
        audio = self.wav_bytes(frames=4_000, sample_rate=16_000)

        async def create(**kwargs):
            return type("Response", (), {"content": audio})()

        modules = self.openai_modules(calls, create)
        with patch.dict(sys.modules, modules), patch(
            "embodied_runtime.voice.time.perf_counter",
            side_effect=[1.0, 2.25, 10.0, 10.4],
        ), patch("embodied_runtime.voice.subprocess.run"), self.assertLogs(
            "embodied_runtime.voice", level="INFO"
        ) as logs:
            provider = FusionHatOpenAITTSProvider()
            await provider.speak("hello")

        self.assertEqual(logs.output, [
            "INFO:embodied_runtime.voice:"
            "[TTS] synthesis_completed duration_ms=1250 audio_ms=250",
            "INFO:embodied_runtime.voice:"
            "[TTS] playback_completed duration_ms=400",
        ])

    async def test_openai_tts_requests_exact_wav_and_controls_speaker(self):
        calls = []

        async def create(**kwargs):
            self.assertEqual(calls[-1], "disable")
            calls.append(("request", kwargs.copy()))
            self.assertEqual(calls[0], "disable")
            return type("Response", (), {"content": b"RIFF wav bytes"})()

        modules = self.openai_modules(calls, create)
        with patch.dict(sys.modules, modules), patch(
            "embodied_runtime.voice.subprocess.run"
        ) as run:
            provider = FusionHatOpenAITTSProvider(
                model="configured-model", voice="configured-voice"
            )
            await provider.speak("Exact **text**\nunchanged")
            await provider.close()
            await provider.speak("second session")

        self.assertEqual(calls[1][1], {
            "model": "configured-model", "voice": "configured-voice",
            "input": "Exact **text**\nunchanged", "response_format": "wav",
        })
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], ["aplay", "--quiet"])
        self.assertEqual(run.call_args_list[0].kwargs["input"], b"RIFF wav bytes")
        self.assertTrue(run.call_args_list[0].kwargs["check"])
        self.assertEqual(calls.count("enable"), 2)
        self.assertGreaterEqual(calls.count("disable"), 5)

    async def test_openai_api_failure_never_plays_or_falls_back(self):
        calls = []

        async def create(**kwargs):
            raise RuntimeError("API failed")

        with patch.dict(sys.modules, self.openai_modules(calls, create)), patch(
            "embodied_runtime.voice.subprocess.run"
        ) as run, patch("embodied_runtime.voice.LOGGER.info") as log:
            provider = FusionHatOpenAITTSProvider()
            with self.assertRaisesRegex(RuntimeError, "API failed"):
                await provider.speak("hello")
        self.assertEqual(calls, ["disable"])
        run.assert_not_called()
        log.assert_not_called()

    async def test_openai_playback_failure_disables_speaker(self):
        calls = []

        async def create(**kwargs):
            return type("Response", (), {"content": b"wav"})()

        with patch.dict(sys.modules, self.openai_modules(calls, create)), patch(
            "embodied_runtime.voice.subprocess.run", side_effect=RuntimeError("failed")
        ), patch("embodied_runtime.voice.LOGGER.info") as log:
            provider = FusionHatOpenAITTSProvider()
            with self.assertRaisesRegex(RuntimeError, "failed"):
                await provider.speak("hello")
        self.assertEqual(calls, ["disable", "enable", "disable"])
        self.assertEqual(log.call_count, 1)
        self.assertEqual(log.call_args.args[0],
                         "[TTS] synthesis_completed duration_ms=%s")

    def test_openai_unavailable_has_install_guidance(self):
        unrelated_openai = ModuleType("openai")
        with patch.dict(sys.modules, {"openai": unrelated_openai}), self.assertRaisesRegex(
            RuntimeError, r"unavailable; install it with: .*\[openai\]"
        ):
            FusionHatOpenAITTSProvider()

    async def test_elevenlabs_collects_complete_wav_and_controls_speaker(self):
        calls = []
        audio = self.wav_bytes(frames=6_000, sample_rate=24_000)

        async def chunks():
            for chunk in (audio[:20], audio[20:]):
                self.assertNotIn("enable", calls)
                yield chunk

        def convert(**kwargs):
            calls.append(("request", kwargs))
            return chunks()

        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "test-key"}), \
             patch.dict(sys.modules, self.elevenlabs_modules(calls, convert)), \
             patch("embodied_runtime.voice.time.perf_counter",
                   side_effect=[1.0, 2.25, 10.0, 10.4]), \
             patch("embodied_runtime.voice.subprocess.run") as run, \
             self.assertLogs("embodied_runtime.voice", level="INFO") as logs:
            provider = FusionHatElevenLabsTTSProvider(
                model="configured-model", voice_id="configured-voice"
            )
            await provider.speak("Exact **text**\nunchanged")
            await provider.close()

        self.assertEqual(calls[1], ("request", {
            "voice_id": "configured-voice", "text": "Exact **text**\nunchanged",
            "model_id": "configured-model", "output_format": "wav_24000",
        }))
        self.assertEqual(run.call_args.args[0], ["aplay", "--quiet"])
        self.assertEqual(run.call_args.kwargs["input"], audio)
        self.assertEqual(logs.output, [
            "INFO:embodied_runtime.voice:[TTS] synthesis_completed "
            "duration_ms=1250 audio_ms=250",
            "INFO:embodied_runtime.voice:[TTS] playback_completed duration_ms=400",
        ])
        self.assertEqual(calls[-3:], ["enable", "disable", "disable"])

    async def test_elevenlabs_generation_and_playback_failures_clean_up(self):
        async def failed_chunks():
            raise RuntimeError("generation failed")
            yield b""  # pragma: no cover

        calls = []
        modules = self.elevenlabs_modules(calls, lambda **kwargs: failed_chunks())
        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "test-key"}), \
             patch.dict(sys.modules, modules), \
             patch("embodied_runtime.voice.subprocess.run") as run:
            provider = FusionHatElevenLabsTTSProvider(voice_id="voice")
            with self.assertRaisesRegex(RuntimeError, "generation failed"):
                await provider.speak("hello")
        self.assertEqual(calls, ["disable"])
        run.assert_not_called()

        calls = []
        async def chunks():
            yield b"wav"
        modules = self.elevenlabs_modules(calls, lambda **kwargs: chunks())
        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "test-key"}), \
             patch.dict(sys.modules, modules), \
             patch("embodied_runtime.voice.subprocess.run",
                   side_effect=RuntimeError("playback failed")), \
             patch("embodied_runtime.voice.LOGGER.info") as log:
            provider = FusionHatElevenLabsTTSProvider(voice_id="voice")
            with self.assertRaisesRegex(RuntimeError, "playback failed"):
                await provider.speak("hello")
        self.assertEqual(calls, ["disable", "enable", "disable"])
        self.assertEqual(log.call_count, 1)

    async def test_elevenlabs_close_preserves_client_across_sessions(self):
        calls = []
        clients = []

        async def chunks():
            yield b"wav"

        modules = self.elevenlabs_modules(calls, lambda **kwargs: chunks())
        client_module = modules["elevenlabs.client"]
        original_factory = client_module.AsyncElevenLabs

        def client_factory(**kwargs):
            client = original_factory(**kwargs)
            clients.append(client)
            return client

        client_module.AsyncElevenLabs = client_factory
        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "test-key"}), \
             patch.dict(sys.modules, modules), \
             patch("embodied_runtime.voice.subprocess.run"):
            provider = FusionHatElevenLabsTTSProvider(voice_id="voice")
            await provider.speak("first")
            await provider.close()
            await provider.speak("second")

        self.assertEqual(len(clients), 1)
        self.assertEqual(calls.count("enable"), 2)
        self.assertEqual(calls.count("disable"), 5)

    def test_elevenlabs_dependency_and_credential_guidance(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(
            RuntimeError, "set ELEVENLABS_API_KEY"
        ):
            FusionHatElevenLabsTTSProvider(voice_id="voice")
        unrelated = ModuleType("elevenlabs.client")
        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "test-key"}), \
             patch.dict(sys.modules, {"elevenlabs.client": unrelated}), \
             self.assertRaisesRegex(RuntimeError, r"install it with: .*\[elevenlabs\]"):
            FusionHatElevenLabsTTSProvider(voice_id="voice")

    def test_piper_unavailable_fails_at_construction_with_install_guidance(self):
        unrelated_piper = ModuleType("piper")
        with tempfile.NamedTemporaryFile(suffix=".onnx") as model, patch.dict(
            sys.modules, {"piper": unrelated_piper}
        ), self.assertRaisesRegex(
            RuntimeError, r"unavailable; install it with: .*\[piper\]"
        ):
            FusionHatPiperTTSProvider(model_path=model.name)

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
