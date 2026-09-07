"""Runtime-owned, bounded voice interaction and optional audio providers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from typing import Protocol


LOGGER = logging.getLogger(__name__)


class VoiceProvider(Protocol):
    """Transient speech I/O used by a runtime-owned voice session."""

    async def listen(self) -> str | None: ...
    async def stop_listening(self) -> None: ...
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
        handle_utterance: Callable[[str], Awaitable[str]],
        policy: VoiceSessionPolicy = VoiceSessionPolicy(),
        *,
        wake_word: str | None = None,
    ) -> None:
        self._provider = provider
        self._handle_utterance = handle_utterance
        self._policy = policy
        self._session_task: asyncio.Task[str] | None = None
        self._listen_task: asyncio.Task[str | None] | None = None
        self._wake_word = wake_word.casefold().strip() if wake_word else None
        self._wake_task: asyncio.Task[None] | None = None
        self._wake_enabled = asyncio.Event()
        self._microphone_lock = asyncio.Lock()
        self._stopping = False
        self._session_pending = False

    @property
    def available(self) -> bool:
        return self._provider is not None

    @property
    def active(self) -> bool:
        return self._session_task is not None and not self._session_task.done()

    @property
    def wake_active(self) -> bool:
        return self._wake_task is not None and not self._wake_task.done()

    async def start(self, *, source: str = "runtime") -> str:
        if self._provider is None:
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
            self._provider is None
            or self._wake_word is None
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
                    "[VOICE] wake_listener word=%r status=ready", self._wake_word
                )
                async with self._microphone_lock:
                    if not self._wake_enabled.is_set() or self._stopping:
                        continue
                    heard = await self._provider.listen()
                if heard is not None and heard.strip().casefold() == self._wake_word:
                    LOGGER.info("[VOICE] wake_detected word=%r", self._wake_word)
                    await self.start(source="wake_word")
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
        reason = "initial_timeout"
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
                await self._provider.speak(response)
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
            finally:
                try:
                    await self._provider.close()
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
    """Lazy adapter over SunFounder's supported Vosk and Espeak components."""

    def __init__(self, *, language: str = "en-us") -> None:
        self._language = language
        self._stt = None
        self._tts = None

    def _ensure_stt(self):
        if self._stt is None:
            try:
                from fusion_hat.stt import Vosk
            except ImportError as error:
                raise RuntimeError("Fusion HAT speech recognition is unavailable") from error
            self._stt = Vosk(language=self._language)
        return self._stt

    def _ensure_tts(self):
        if self._tts is None:
            try:
                from fusion_hat.tts import Espeak
            except ImportError as error:
                raise RuntimeError("Fusion HAT speech synthesis is unavailable") from error
            self._tts = Espeak()
        return self._tts

    async def listen(self) -> str | None:
        return await asyncio.to_thread(self._listen_sync)

    def _listen_sync(self) -> str | None:
        return self._ensure_stt().listen()

    async def stop_listening(self) -> None:
        if self._stt is not None:
            await asyncio.to_thread(self._stt.stop_listening)

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
