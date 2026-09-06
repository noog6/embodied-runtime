import asyncio
import sys
from types import ModuleType
import unittest
from unittest.mock import AsyncMock, patch

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

    async def close(self):
        self.close_calls += 1


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
