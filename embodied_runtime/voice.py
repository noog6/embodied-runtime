"""Runtime-owned, bounded voice interaction and optional audio providers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import importlib
import io
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Protocol, TypeVar
import wave

from embodied_runtime.earcons import (
    Earcon, EarconPlayer, FusionHatEarconOutput, earcon_wav,
)

from embodied_runtime.observability import RunObservability
from embodied_runtime.resources import (
    ResourceArbiter,
    ResourceBusyError,
    ResourceKey,
    ResourceOwner,
)


LOGGER = logging.getLogger(__name__)

_WAKE_CAPTURE_STOP_RETRY_SECONDS = 0.1
_WAKE_CAPTURE_STOP_TIMEOUT_SECONDS = 2.0
_WAKE_RESOURCE_BUSY_BACKOFF_SECONDS = 0.1
_T = TypeVar("_T")

MICROPHONE_RESOURCE = ResourceKey("audio.microphone")
VOICE_MICROPHONE_OWNER = ResourceOwner("runtime", "voice")
VOICE_WAKE_MICROPHONE_OWNER = ResourceOwner("runtime", "voice_wake")


async def _await_owned_blocking_operation(operation: Callable[[], _T]) -> _T:
    """Wait for owned blocking work to really finish before cancellation exits."""
    loop = asyncio.get_running_loop()
    worker = loop.run_in_executor(None, operation)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Cancellation cannot stop executor work. Keep ownership until this exact
        # callable exits, tolerating repeated cancellation, then propagate it.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        raise


class PiperTTSUnavailableError(RuntimeError):
    """Raised when selected local Piper speech cannot be initialized."""


class OpenAITTSUnavailableError(RuntimeError):
    """Raised when selected hosted OpenAI speech cannot be initialized."""


class ElevenLabsTTSUnavailableError(RuntimeError):
    """Raised when selected hosted ElevenLabs speech cannot be initialized."""


@dataclass(frozen=True, slots=True)
class TextToSpeechResult:
    provider: str


@dataclass(frozen=True, slots=True)
class SynthesisFailureDetails:
    provider: str
    error: str
    http_status: int | None = None
    provider_type: str | None = None
    code: str | None = None
    message: str | None = None
    request_id: str | None = None
    reason: str = "provider_error"


class TextToSpeechSynthesisError(RuntimeError):
    """Safe semantic boundary for hosted synthesis (never local playback)."""

    def __init__(self, details: SynthesisFailureDetails) -> None:
        super().__init__(details.reason)
        self.details = details


def _safe_provider_text(value: object, limit: int = 240) -> str | None:
    if not isinstance(value, (str, int)):
        return None
    normalized = " ".join(str(value).split())
    normalized = re.sub(r"https?://\S+", "[redacted-url]", normalized)
    normalized = re.sub(
        r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?\S+",
        "authorization=[redacted]", normalized,
    )
    normalized = re.sub(
        r"(?i)\b(xi-api-key|api[_ -]?key)\s*[:=]\s*\S+",
        r"\1=[redacted]", normalized,
    )
    normalized = re.sub(
        r"(?i)\bbearer\s+\S+", "bearer [redacted]", normalized
    )
    return normalized[:limit] or None


def elevenlabs_failure_details(error: BaseException) -> SynthesisFailureDetails:
    """Extract only explicitly allowed fields from known SDK error containers."""
    body = getattr(error, "body", None)
    detail = body.get("detail") if isinstance(body, dict) else None
    source = detail if isinstance(detail, dict) else (
        body if isinstance(body, dict) else {}
    )

    def allowed(name: str) -> object:
        value = source.get(name) if isinstance(source, dict) else None
        return value if value is not None else getattr(error, name, None)

    status = getattr(error, "status_code", None)
    if type(status) is not int:
        status = None
    provider_type = _safe_provider_text(allowed("type"))
    code = _safe_provider_text(allowed("code"))
    message = _safe_provider_text(allowed("message"))
    request_id = _safe_provider_text(
        allowed("request_id"), 96
    )
    exhausted = code in {"insufficient_credits", "quota_exceeded"}
    return SynthesisFailureDetails(
        "elevenlabs", type(error).__name__, status, provider_type, code,
        message, request_id, "credits_exhausted" if exhausted else "provider_error",
    )


class VoiceProvider(Protocol):
    """Transient speech input for a voice session."""

    async def listen(self) -> str | None: ...
    async def stop_listening(self) -> None: ...
    async def close(self) -> None: ...


class TextToSpeechProvider(Protocol):
    """Speech output used by a runtime-owned voice session."""

    async def speak(self, text: str) -> TextToSpeechResult | None: ...
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
        resources: ResourceArbiter | None = None,
        observability: RunObservability | None = None,
        earcons: EarconPlayer | None = None,
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
        self._wake_listen_task: asyncio.Task[str | None] | None = None
        self._wake_enabled = asyncio.Event()
        self._microphone_lock = asyncio.Lock()
        self._resources = resources if resources is not None else ResourceArbiter()
        self._observability = observability
        self._earcons = earcons
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
            wake_capture = self._wake_listen_task
            if (
                wake_capture is not None
                and not wake_capture.done()
                and self._provider is not None
            ):
                await self._provider.stop_listening()
            async with self._microphone_lock:
                try:
                    lease = self._resources.acquire(
                        MICROPHONE_RESOURCE, VOICE_MICROPHONE_OWNER
                    )
                except ResourceBusyError:
                    return "Voice interaction failed: microphone resource is busy."
                try:
                    self._session_task = asyncio.create_task(
                        self._run(source), name="bounded-voice-session"
                    )
                    try:
                        return await self._session_task
                    finally:
                        self._session_task = None
                finally:
                    self._resources.release(lease)
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
        ignored_huhs = 0
        LOGGER.info(
            "[VOICE] wake_listener words=%r status=ready",
            sorted(self._wake_words),
        )
        try:
            while not self._stopping:
                await self._wake_enabled.wait()
                if self._stopping:
                    break
                async with self._microphone_lock:
                    if not self._wake_enabled.is_set() or self._stopping:
                        continue
                    try:
                        lease = self._resources.acquire(
                            MICROPHONE_RESOURCE, VOICE_WAKE_MICROPHONE_OWNER
                        )
                    except ResourceBusyError:
                        lease = None
                    if lease is not None:
                        self._wake_listen_task = asyncio.create_task(
                            self._provider.listen(), name="voice-wake-capture"
                        )
                        if self._observability is not None:
                            self._observability.increment("wake_capture_attempts")
                        try:
                            heard = await asyncio.shield(self._wake_listen_task)
                        finally:
                            await asyncio.gather(
                                self._wake_listen_task, return_exceptions=True
                            )
                            self._wake_listen_task = None
                            self._resources.release(lease)
                if lease is None:
                    await asyncio.sleep(_WAKE_RESOURCE_BUSY_BACKOFF_SECONDS)
                    continue
                normalized = heard.strip().casefold() if heard is not None else ""
                if normalized == "huh":
                    ignored_huhs += 1
                elif normalized in self._wake_words:
                    if self._observability is not None:
                        self._observability.increment("wake_captures")
                    LOGGER.info("[VOICE] wake_detected heard=%r", heard.strip())
                    await self.start(source="wake_word")
                elif normalized:
                    LOGGER.debug(
                        "[VOICE] wake_rejected text=%r",
                        heard.strip(),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.warning(
                "[VOICE] wake_listener_failed error=%s", type(error).__name__
            )
        finally:
            LOGGER.info(
                "[VOICE] wake_listener status=stopped ignored_huhs=%s",
                ignored_huhs,
            )

    async def stop(self) -> None:
        self._stopping = True
        self._wake_enabled.set()
        stop_error: Exception | None = None
        wake_capture = self._wake_listen_task
        session_capture = self._listen_task
        try:
            if self._provider is not None and any(
                capture is not None and not capture.done()
                for capture in (wake_capture, session_capture)
            ):
                await self._provider.stop_listening()
        except Exception as error:
            stop_error = error
            LOGGER.exception("[VOICE] capture_stop_failed")
        wake_task = self._wake_task
        wake_capture_timed_out = False
        if wake_task is not None:
            wake_listen_task = self._wake_listen_task
            if wake_listen_task is not None and not wake_listen_task.done():
                capture_error = await self._stop_and_join_wake_capture(
                    wake_listen_task
                )
                if capture_error is not None:
                    if stop_error is None:
                        stop_error = capture_error
                    wake_capture_timed_out = isinstance(capture_error, TimeoutError)
        task = self._session_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if wake_task is not None and not wake_capture_timed_out:
            await asyncio.gather(wake_task, return_exceptions=True)
            self._wake_task = None
        if stop_error is not None:
            raise stop_error

    async def _stop_and_join_wake_capture(
        self, wake_listen_task: asyncio.Task[str | None]
    ) -> Exception | None:
        """Cooperatively stop an owned wake capture within a fixed deadline."""
        assert self._provider is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _WAKE_CAPTURE_STOP_TIMEOUT_SECONDS
        stop_error: Exception | None = None
        while not wake_listen_task.done():
            try:
                await self._provider.stop_listening()
            except Exception as error:
                if stop_error is None:
                    stop_error = error
                LOGGER.exception("[VOICE] wake_capture_stop_failed")

            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(
                    asyncio.shield(wake_listen_task),
                    min(_WAKE_CAPTURE_STOP_RETRY_SECONDS, remaining),
                )
            except TimeoutError:
                continue

        if not wake_listen_task.done():
            timeout_error = TimeoutError(
                "wake capture did not stop within "
                f"{_WAKE_CAPTURE_STOP_TIMEOUT_SECONDS:g} seconds"
            )
            LOGGER.error("[VOICE] wake_capture_stop_timeout")
            return timeout_error
        await asyncio.gather(wake_listen_task, return_exceptions=True)
        return stop_error

    async def _run(self, source: str) -> str:
        assert self._provider is not None
        assert self._text_to_speech_provider is not None
        reason = "initial_timeout"
        if source == "wake_word":
            if self._earcons is not None:
                await self._earcons.play(Earcon.ENGAGEMENT)
            else:
                # Compatibility for independently constructed voice sessions;
                # RobotApplication always supplies the runtime earcon facility.
                try:
                    await self._provider.play_engagement_cue()  # type: ignore[attr-defined]
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
                if self._observability is not None:
                    self._observability.increment("stt_captures")
                LOGGER.info("[VOICE] heard turn=%s text=%r", turn, text)
                LOGGER.info("[VOICE] thinking turn=%s", turn)
                response = await self._handle_utterance(text)
                LOGGER.info("[VOICE] response turn=%s text=%r", turn, response)
                LOGGER.info("[VOICE] speaking turn=%s", turn)
                await self._text_to_speech_provider.speak(response)
                if self._observability is not None:
                    self._observability.increment("voice_turns")
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
    """Lazy adapter over SunFounder's Vosk speech input."""

    def __init__(self, *, language: str = "en-us") -> None:
        self._language = language
        self._stt = None
        self._listen_state_lock = threading.Lock()
        self._listen_stop: threading.Event | None = None

    def _ensure_stt(self):
        with self._listen_state_lock:
            stt = self._stt
        if stt is None:
            try:
                from fusion_hat.stt import Vosk
            except ImportError as error:
                raise RuntimeError("Fusion HAT speech recognition is unavailable") from error
            initialized = Vosk(language=self._language)
            with self._listen_state_lock:
                if self._stt is None:
                    self._stt = initialized
                stt = self._stt
        return stt

    async def listen(self) -> str | None:
        stop = threading.Event()
        with self._listen_state_lock:
            if self._listen_stop is not None:
                raise RuntimeError("speech recognition is already listening")
            self._listen_stop = stop
        # An executor Future is not a separate Task for Runner.cancel-all to
        # cancel independently of this coroutine's ownership of the thread.
        loop = asyncio.get_running_loop()
        worker = loop.run_in_executor(None, self._listen_sync, stop)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Cancellation does not stop executor work.  Continue shielding
            # until the callable really returns, including across repeat
            # cancellation requests, before reporting owner cancellation.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
            raise
        finally:
            with self._listen_state_lock:
                if self._listen_stop is stop:
                    self._listen_stop = None

    def _listen_sync(self, stop: threading.Event) -> str | None:
        stt = self._ensure_stt()
        # A stop requested while lazy construction was in progress belongs to
        # this attempt and prevents entry into a fresh blocking capture.
        if stop.is_set():
            return None
        return stt.listen()

    async def stop_listening(self) -> None:
        with self._listen_state_lock:
            stop = self._listen_stop
            if stop is not None:
                stop.set()
            stt = self._stt
        if stt is not None:
            await _await_owned_blocking_operation(stt.stop_listening)

    async def play_engagement_cue(self) -> None:
        """Compatibility shim; runtime wake handling now uses ``EarconPlayer``."""
        await FusionHatEarconOutput().play_wav(earcon_wav(Earcon.ENGAGEMENT))

    async def close(self) -> None:
        """Release voice-input resources (Vosk has no separate close operation)."""


class FusionHatEspeakTTSProvider:
    """Lazy SunFounder eSpeak output using the Fusion HAT speaker."""

    def __init__(self) -> None:
        self._tts = None
        self.identifier = "espeak"

    def _ensure_tts(self):
        if self._tts is None:
            try:
                from fusion_hat.tts import Espeak
            except ImportError as error:
                raise RuntimeError("Fusion HAT speech synthesis is unavailable") from error
            self._tts = Espeak()
        return self._tts

    async def speak(self, text: str) -> None:
        await _await_owned_blocking_operation(lambda: self._speak_sync(text))

    def _speak_sync(self, text: str) -> None:
        try:
            from fusion_hat.device import disable_speaker, enable_speaker
        except ImportError as error:
            raise RuntimeError("Fusion HAT speaker control is unavailable") from error
        enable_speaker()
        try:
            self._ensure_tts().say(text)
        finally:
            disable_speaker()

    async def close(self) -> None:
        try:
            from fusion_hat.device import disable_speaker
        except ImportError:
            return
        await _await_owned_blocking_operation(disable_speaker)


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
        await _await_owned_blocking_operation(lambda: self._speak_sync(text))

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
        await _await_owned_blocking_operation(disable_speaker)


class FusionHatOpenAITTSProvider:
    """Reusable hosted OpenAI speech synthesis with Fusion HAT playback."""

    def __init__(
        self, *, model: str = "gpt-4o-mini-tts", voice: str = "cedar"
    ) -> None:
        try:
            AsyncOpenAI = getattr(importlib.import_module("openai"), "AsyncOpenAI")
        except (ImportError, AttributeError) as error:
            raise OpenAITTSUnavailableError(
                "OpenAI speech synthesis is unavailable; install it with: "
                "python -m pip install -e '.[openai]'"
            ) from error
        self._model = model
        self._voice = voice
        self._client = AsyncOpenAI()

    async def speak(self, text: str) -> None:
        try:
            from fusion_hat.device import disable_speaker, enable_speaker
        except ImportError as error:
            raise RuntimeError("Fusion HAT speaker control is unavailable") from error

        # A previous failed session must not leave amplification active during I/O.
        await _await_owned_blocking_operation(disable_speaker)
        synthesis_started = time.perf_counter()
        response = await self._client.audio.speech.create(
            model=self._model,
            voice=self._voice,
            input=text,
            response_format="wav",
        )
        synthesis_ms = int((time.perf_counter() - synthesis_started) * 1_000)
        wav_bytes = response.content
        _log_synthesis_completed(synthesis_ms, wav_bytes)

        def play() -> None:
            enable_speaker()
            try:
                playback_started = time.perf_counter()
                subprocess.run(
                    ["aplay", "--quiet"], input=wav_bytes, check=True
                )
                playback_ms = int(
                    (time.perf_counter() - playback_started) * 1_000
                )
            finally:
                disable_speaker()
            LOGGER.info("[TTS] playback_completed duration_ms=%s", playback_ms)

        await _await_owned_blocking_operation(play)

    async def close(self) -> None:
        """Disable output while retaining the reusable OpenAI client."""
        try:
            from fusion_hat.device import disable_speaker
        except ImportError:
            return
        await _await_owned_blocking_operation(disable_speaker)


class FusionHatElevenLabsTTSProvider:
    """Reusable hosted ElevenLabs synthesis with Fusion HAT playback."""

    def __init__(
        self,
        *,
        voice_id: str,
        model: str = "eleven_flash_v2_5",
        speed: float = 1.0,
        observability: RunObservability | None = None,
    ) -> None:
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            raise ElevenLabsTTSUnavailableError(
                "ElevenLabs speech synthesis is unavailable; set ELEVENLABS_API_KEY"
            )
        try:
            AsyncElevenLabs = getattr(
                importlib.import_module("elevenlabs.client"), "AsyncElevenLabs"
            )
            VoiceSettings = getattr(
                importlib.import_module("elevenlabs"), "VoiceSettings"
            )
        except (ImportError, AttributeError) as error:
            raise ElevenLabsTTSUnavailableError(
                "ElevenLabs speech synthesis is unavailable; install it with: "
                "python -m pip install -e '.[elevenlabs]'"
            ) from error
        self._model = model
        self._voice_id = voice_id
        self._speed = speed
        self._voice_settings_type = VoiceSettings
        self._client = AsyncElevenLabs(api_key=api_key)
        self._observability = observability
        self.identifier = "elevenlabs"

    async def speak(self, text: str) -> None:
        try:
            from fusion_hat.device import disable_speaker, enable_speaker
        except ImportError as error:
            raise RuntimeError("Fusion HAT speaker control is unavailable") from error

        await _await_owned_blocking_operation(disable_speaker)
        synthesis_started = time.perf_counter()
        billed_characters = len(text)
        usage_basis = "text_length_estimate"
        try:
            async with self._client.text_to_speech.with_raw_response.convert(
                    voice_id=self._voice_id, text=text, model_id=self._model,
                    output_format="wav_24000",
                    voice_settings=self._voice_settings_type(speed=self._speed),
            ) as response:
                wav_bytes = b"".join([chunk async for chunk in response.data])
                try:
                    header = response.headers.get("character-cost")
                    if (isinstance(header, str) and len(header) <= 7
                            and header.isascii() and header.isdigit()):
                        reported = int(header)
                        if reported <= 1_000_000:
                            billed_characters = reported
                            usage_basis = "provider_reported"
                except Exception:
                    # Billing metadata must never decide whether speech succeeds.
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as error:
            details = elevenlabs_failure_details(error)
            fields = {"http_status": details.http_status,
                      "type": details.provider_type, "code": details.code,
                      "reason": details.reason, "detail": details.message,
                      "request_id": details.request_id}
            LOGGER.warning(
                "[TTS] provider=elevenlabs stage=synthesis status=failed "
                "error=%s %s", details.error,
                " ".join(f"{key}={value!r}" for key, value in fields.items()
                         if value is not None),
            )
            if self._observability is not None:
                self._observability.event(
                    "voice", "tts_synthesis", "failed", severity="error",
                    source="elevenlabs", error=details.error,
                    metadata={key: value for key, value in fields.items()
                              if value is not None},
                )
            raise TextToSpeechSynthesisError(details) from error
        synthesis_ms = int((time.perf_counter() - synthesis_started) * 1_000)
        _log_synthesis_completed(synthesis_ms, wav_bytes)
        if self._observability is not None:
            self._observability.tts_synthesized(
                "elevenlabs", self._model, characters=billed_characters,
                duration_ms=synthesis_ms, usage_basis=usage_basis,
            )

        def play() -> None:
            enable_speaker()
            try:
                playback_started = time.perf_counter()
                subprocess.run(["aplay", "--quiet"], input=wav_bytes, check=True)
                playback_ms = int(
                    (time.perf_counter() - playback_started) * 1_000
                )
            finally:
                disable_speaker()
            LOGGER.info("[TTS] playback_completed duration_ms=%s", playback_ms)

        await _await_owned_blocking_operation(play)
        return TextToSpeechResult("elevenlabs")

    async def close(self) -> None:
        """Disable output while retaining the reusable ElevenLabs client."""
        try:
            from fusion_hat.device import disable_speaker
        except ImportError:
            return
        await _await_owned_blocking_operation(disable_speaker)


class FallbackTextToSpeechProvider:
    """One lazy local fallback, solely for hosted synthesis failures."""

    def __init__(self, primary: TextToSpeechProvider,
                 fallback_factory: Callable[[], TextToSpeechProvider], *,
                 fallback_identifier: str = "espeak",
                 observability: RunObservability | None = None) -> None:
        self._primary = primary
        self._fallback_factory = fallback_factory
        self._fallback_identifier = fallback_identifier
        self._fallback: TextToSpeechProvider | None = None
        self._observability = observability

    async def speak(self, text: str) -> TextToSpeechResult:
        try:
            result = await self._primary.speak(text)
            return result or TextToSpeechResult(
                str(getattr(self._primary, "identifier", "unknown")))
        except TextToSpeechSynthesisError as error:
            if self._observability is not None:
                self._observability.increment("tts_primary_failures")
                self._observability.increment("tts_fallbacks")
                self._observability.event(
                    "voice", "tts_fallback", "attempted",
                    source=self._fallback_identifier,
                    metadata={"primary": error.details.provider,
                              "reason": error.details.reason},
                )
            LOGGER.warning("[TTS] primary=%s fallback=%s status=attempted reason=%s",
                           error.details.provider, self._fallback_identifier,
                           error.details.reason)
            if self._fallback is None:
                self._fallback = self._fallback_factory()
            try:
                await self._fallback.speak(text)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._observability is not None:
                    self._observability.increment("tts_fallback_failures")
                raise
            LOGGER.info("[TTS] provider=%s fallback=true status=completed",
                        self._fallback_identifier)
            return TextToSpeechResult(self._fallback_identifier)

    async def close(self) -> None:
        primary_error: BaseException | None = None
        try:
            await self._primary.close()
        except BaseException as error:
            primary_error = error
        fallback_error: BaseException | None = None
        if self._fallback is not None:
            try:
                await self._fallback.close()
            except BaseException as error:
                fallback_error = error
        if primary_error is not None:
            raise primary_error
        if fallback_error is not None:
            raise fallback_error


def _log_synthesis_completed(synthesis_ms: int, wav_bytes: bytes) -> None:
    """Log hosted synthesis timing without making WAV inspection operational."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            frame_size = wav.getnchannels() * wav.getsampwidth()
            sample_rate = wav.getframerate()
            if frame_size <= 0 or sample_rate <= 0:
                raise wave.Error("invalid WAV format")
            actual_audio_bytes = 0
            while frames := wav.readframes(4096):
                actual_audio_bytes += len(frames)
            actual_frames = actual_audio_bytes // frame_size
            audio_ms = int(actual_frames * 1_000 / sample_rate)
    except (EOFError, wave.Error, ZeroDivisionError):
        LOGGER.info("[TTS] synthesis_completed duration_ms=%s", synthesis_ms)
    else:
        LOGGER.info(
            "[TTS] synthesis_completed duration_ms=%s audio_ms=%s",
            synthesis_ms,
            audio_ms,
        )
