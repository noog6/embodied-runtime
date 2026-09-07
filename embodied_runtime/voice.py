"""Runtime-owned, bounded voice interaction and optional audio providers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import importlib
import io
import logging
import math
from pathlib import Path
import struct
import subprocess
from typing import Protocol
import wave


LOGGER = logging.getLogger(__name__)


class PiperTTSUnavailableError(RuntimeError):
    """Raised when selected local Piper speech cannot be initialized."""


class VoiceProvider(Protocol):
    """Transient speech input and wake acknowledgement for a voice session."""

    async def listen(self) -> str | None: ...
    async def stop_listening(self) -> None: ...
    async def play_engagement_cue(self) -> None: ...
    async def close(self) -> None: ...


class TextToSpeechProvider(Protocol):
    """Speech output used by a runtime-owned voice session."""

    async def speak(self, text: str) -> None: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class VoiceSessionPolicy:
    initial_timeout_seconds: float = 18.0
    followup_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.initial_timeout_seconds <= 0 or self.followup_timeout_seconds <= 0:
            raise ValueError("voice timeouts must be positive")


class VoiceInteraction:
    """Coordinate at most two half-duplex operator speech turns."""

    def __init__(
        self,
        provider: VoiceProvider | None,
        text_to_speech_provider: TextToSpeechProvider | None,
        handle_utterance: Callable[[str], Awaitable[str]],
        policy: VoiceSessionPolicy = VoiceSessionPolicy(),
        *,
        wake_words: list[str] | None = None,
    ) -> None:
        self._provider = provider
        self._text_to_speech_provider = text_to_speech_provider
        self._handle_utterance = handle_utterance
        self._policy = policy
        self._session_task: asyncio.Task[str] | None = None
        self._listen_task: asyncio.Task[str | None] | None = None
        self._wake_words = frozenset(
            word.strip().casefold() for word in wake_words or ()
        )
        self._wake_task: asyncio.Task[None] | None = None
        self._wake_enabled = asyncio.Event()
        self._microphone_lock = asyncio.Lock()
        self._stopping = False
        self._session_pending = False

    @property
    def available(self) -> bool:
        return (
            self._provider is not None
            and self._text_to_speech_provider is not None
        )

    @property
    def active(self) -> bool:
        return self._session_task is not None and not self._session_task.done()

    @property
    def wake_active(self) -> bool:
        return self._wake_task is not None and not self._wake_task.done()

    async def start(self, *, source: str = "runtime") -> str:
        if not self.available:
            return "Voice interaction unavailable."
        if self.active or self._session_pending:
            return "Voice interaction already active."
        self._session_pending = True
        try:
            self._wake_enabled.clear()
            # Cooperatively release a wake capture before waiting for ownership.
            if self._wake_task is not None and self._provider is not None:
                await self._provider.stop_listening()
            async with self._microphone_lock:
                self._session_task = asyncio.create_task(
                    self._run(source), name="bounded-voice-session"
                )
                try:
                    return await self._session_task
                finally:
                    self._session_task = None
        finally:
            self._session_pending = False
            if not self._stopping and self._wake_task is not None:
                self._wake_enabled.set()

    def start_wake_listener(self) -> None:
        """Start application-owned local wake recognition when configured."""
        if (
            not self.available
            or not self._wake_words
            or self._wake_task is not None
        ):
            return
        self._stopping = False
        self._wake_enabled.set()
        self._wake_task = asyncio.create_task(
            self._run_wake_listener(), name="voice-wake-listener"
        )

    async def _run_wake_listener(self) -> None:
        try:
            while not self._stopping:
                await self._wake_enabled.wait()
                if self._stopping:
                    break
                LOGGER.info(
                    "[VOICE] wake_listener words=%r status=ready",
                    sorted(self._wake_words),
                )
                async with self._microphone_lock:
                    if not self._wake_enabled.is_set() or self._stopping:
                        continue
                    heard = await self._provider.listen()
                normalized = heard.strip().casefold() if heard is not None else ""
                if normalized in self._wake_words:
                    LOGGER.info("[VOICE] wake_detected heard=%r", heard.strip())
                    await self.start(source="wake_word")
                elif normalized:
                    LOGGER.info(
                        "[VOICE] wake_rejected text=%r",
                        heard.strip(),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.warning(
                "[VOICE] wake_listener_failed error=%s", type(error).__name__
            )

    async def stop(self) -> None:
        self._stopping = True
        self._wake_enabled.set()
        stop_error: Exception | None = None
        try:
            if self._provider is not None:
                await self._provider.stop_listening()
        except Exception as error:
            stop_error = error
            LOGGER.exception("[VOICE] capture_stop_failed")
        wake_task = self._wake_task
        if wake_task is not None:
            wake_task.cancel()
            await asyncio.gather(wake_task, return_exceptions=True)
            self._wake_task = None
        task = self._session_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if stop_error is not None:
            raise stop_error

    async def _run(self, source: str) -> str:
        assert self._provider is not None
        assert self._text_to_speech_provider is not None
        reason = "initial_timeout"
        if source == "wake_word":
            try:
                await self._provider.play_engagement_cue()
            except Exception as error:
                LOGGER.warning(
                    "[VOICE] engagement_cue status=failed error=%s",
                    type(error).__name__,
                )
            else:
                LOGGER.info("[VOICE] engagement_cue status=played")
        LOGGER.info("[VOICE] session_started source=%s", source)
        try:
            for turn, timeout in (
                (1, self._policy.initial_timeout_seconds),
                (2, self._policy.followup_timeout_seconds),
            ):
                if turn == 2:
                    LOGGER.info("[VOICE] awaiting_followup timeout_s=%s", timeout)
                LOGGER.info("[VOICE] listening turn=%s", turn)
                text = await self._listen(timeout)
                if not text or not text.strip():
                    reason = "initial_timeout" if turn == 1 else "followup_timeout"
                    LOGGER.info("[VOICE] timeout turn=%s", turn)
                    return "Voice session closed."
                text = text.strip()
                LOGGER.info("[VOICE] heard turn=%s text=%r", turn, text)
                LOGGER.info("[VOICE] thinking turn=%s", turn)
                response = await self._handle_utterance(text)
                LOGGER.info("[VOICE] response turn=%s text=%r", turn, response)
                LOGGER.info("[VOICE] speaking turn=%s", turn)
                await self._text_to_speech_provider.speak(response)
                reason = "max_turns" if turn == 2 else reason
            return "Voice session closed."
        except asyncio.CancelledError:
            reason = "shutdown"
            raise
        except Exception as error:
            reason = "error"
            LOGGER.warning("[VOICE] session_failed error=%s", type(error).__name__)
            return f"Voice interaction failed: {error}."
        finally:
            try:
                await self._provider.stop_listening()
            except Exception:
                LOGGER.exception("[VOICE] capture_cleanup_failed")
            try:
                await self._provider.close()
            except Exception:
                LOGGER.exception("[VOICE] input_cleanup_failed")
            try:
                await self._text_to_speech_provider.close()
            except Exception:
                LOGGER.exception("[VOICE] speaker_cleanup_failed")
            LOGGER.info("[VOICE] session_closed reason=%s", reason)

    async def _listen(self, timeout: float) -> str | None:
        assert self._provider is not None
        self._listen_task = asyncio.create_task(self._provider.listen())
        try:
            return await asyncio.wait_for(asyncio.shield(self._listen_task), timeout)
        except TimeoutError:
            stop_error = await self._stop_and_join_listen()
            if stop_error is not None:
                raise stop_error
            return None
        except asyncio.CancelledError:
            await self._stop_and_join_listen()
            raise
        finally:
            self._listen_task = None

    async def _stop_and_join_listen(self) -> Exception | None:
        """Attempt capture release without abandoning the active listen task."""
        assert self._provider is not None
        assert self._listen_task is not None
        stop_error: Exception | None = None
        try:
            await self._provider.stop_listening()
        except Exception as error:
            stop_error = error
            LOGGER.exception("[VOICE] capture_stop_failed")
            self._listen_task.cancel()
        # A successful vendor stop cooperatively completes capture. If it failed,
        # cancellation still ensures the asyncio task is explicitly joined.
        await asyncio.gather(self._listen_task, return_exceptions=True)
        return stop_error


class FusionHatVoiceProvider:
    """Lazy adapter over SunFounder's Vosk and local wake acknowledgement."""

    def __init__(self, *, language: str = "en-us") -> None:
        self._language = language
        self._stt = None

    def _ensure_stt(self):
        if self._stt is None:
            try:
                from fusion_hat.stt import Vosk
            except ImportError as error:
                raise RuntimeError("Fusion HAT speech recognition is unavailable") from error
            self._stt = Vosk(language=self._language)
        return self._stt

    async def listen(self) -> str | None:
        return await asyncio.to_thread(self._listen_sync)

    def _listen_sync(self) -> str | None:
        return self._ensure_stt().listen()

    async def stop_listening(self) -> None:
        if self._stt is not None:
            await asyncio.to_thread(self._stt.stop_listening)

    async def play_engagement_cue(self) -> None:
        """Play the fixed local wake acknowledgement through the HAT speaker."""
        await asyncio.to_thread(self._play_engagement_cue_sync)

    def _play_engagement_cue_sync(self) -> None:
        try:
            from fusion_hat.device import disable_speaker, enable_speaker
        except ImportError as error:
            raise RuntimeError("Fusion HAT speaker control is unavailable") from error
        enable_speaker()
        try:
            subprocess.run(
                ["aplay", "--quiet"],
                input=_engagement_cue_wav(),
                check=True,
            )
        finally:
            disable_speaker()

    async def close(self) -> None:
        """Release voice-input resources (Vosk has no separate close operation)."""


class FusionHatEspeakTTSProvider:
    """Lazy SunFounder eSpeak output using the Fusion HAT speaker."""

    def __init__(self) -> None:
        self._tts = None

    def _ensure_tts(self):
        if self._tts is None:
            try:
                from fusion_hat.tts import Espeak
            except ImportError as error:
                raise RuntimeError("Fusion HAT speech synthesis is unavailable") from error
            self._tts = Espeak()
        return self._tts

    async def speak(self, text: str) -> None:
        await asyncio.to_thread(self._speak_sync, text)

    def _speak_sync(self, text: str) -> None:
        try:
            from fusion_hat.device import enable_speaker
        except ImportError as error:
            raise RuntimeError("Fusion HAT speaker control is unavailable") from error
        enable_speaker()
        self._ensure_tts().say(text)

    async def close(self) -> None:
        try:
            from fusion_hat.device import disable_speaker
        except ImportError:
            return
        await asyncio.to_thread(disable_speaker)


class FusionHatPiperTTSProvider:
    """Lazy, reusable Piper voice with Fusion HAT speaker playback."""

    def __init__(self, *, model_path: str | Path) -> None:
        try:
            PiperVoice = getattr(importlib.import_module("piper"), "PiperVoice")
        except (ImportError, AttributeError) as error:
            raise PiperTTSUnavailableError(
                "Piper speech synthesis is unavailable; install it with: "
                "python -m pip install -e '.[piper]'"
            ) from error
        self._model_path = Path(model_path).expanduser()
        if not self._model_path.is_file():
            raise PiperTTSUnavailableError(
                f"Piper model file not found: {self._model_path}"
            )
        self._piper_voice_class = PiperVoice
        self._voice = None

    def _ensure_voice(self):
        if self._voice is None:
            self._voice = self._piper_voice_class.load(str(self._model_path))
        return self._voice

    async def speak(self, text: str) -> None:
        await asyncio.to_thread(self._speak_sync, text)

    def _speak_sync(self, text: str) -> None:
        output = io.BytesIO()
        wav_writer = wave.open(output, "wb")
        try:
            self._ensure_voice().synthesize_wav(text, wav_writer)
        except Exception:
            # An early synthesis error can leave wave without enough format
            # information to close cleanly; do not mask the useful Piper error.
            try:
                wav_writer.close()
            except wave.Error:
                pass
            raise
        else:
            wav_writer.close()
        wav_bytes = output.getvalue()

        try:
            from fusion_hat.device import disable_speaker, enable_speaker
        except ImportError as error:
            raise RuntimeError("Fusion HAT speaker control is unavailable") from error
        enable_speaker()
        try:
            subprocess.run(["aplay", "--quiet"], input=wav_bytes, check=True)
        finally:
            disable_speaker()

    async def close(self) -> None:
        """Disable physical output without unloading the cached neural voice."""
        try:
            from fusion_hat.device import disable_speaker
        except ImportError:
            return
        await asyncio.to_thread(disable_speaker)


def _engagement_cue_wav() -> bytes:
    """Return a 220 ms, two-note PCM WAV acknowledgement (880 then 1,175 Hz)."""
    sample_rate = 16_000
    amplitude = 7_000
    note_seconds = 0.1
    gap_seconds = 0.02
    ramp_samples = int(sample_rate * 0.01)
    samples: list[int] = []
    for index, frequency in enumerate((880.0, 1_175.0)):
        note_samples = int(sample_rate * note_seconds)
        for position in range(note_samples):
            edge = min(position + 1, note_samples - position, ramp_samples)
            envelope = edge / ramp_samples
            sample = amplitude * envelope * math.sin(
                2.0 * math.pi * frequency * position / sample_rate
            )
            samples.append(round(sample))
        if index == 0:
            samples.extend([0] * int(sample_rate * gap_seconds))

    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return output.getvalue()
