"""Deterministic, runtime-owned semantic audio cues."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import io
import logging
import math
import struct
import subprocess
from typing import Protocol, TypeVar
import wave

from embodied_runtime.resources import (
    ResourceArbiter, ResourceBusyError, ResourceKey, ResourceOwner,
)


LOGGER = logging.getLogger(__name__)
SPEAKER_RESOURCE = ResourceKey("audio.speaker")
EARCON_SPEAKER_OWNER = ResourceOwner("runtime", "earcons")
_T = TypeVar("_T")


class Earcon(StrEnum):
    """The deliberately small semantic cue vocabulary."""

    ENGAGEMENT = "engagement"
    READY = "ready"
    WORK_STARTED = "work_started"
    WORK_COMPLETED = "work_completed"
    NEEDS_OPERATOR = "needs_operator"


@dataclass(frozen=True, slots=True)
class EarconDefinition:
    """One runtime cue and its bounded semantic/model-facing meaning."""

    cue: Earcon
    meaning: str
    notes: tuple[float, ...]


EARCON_CATALOG = (
    EarconDefinition(
        Earcon.ENGAGEMENT, "a local voice interaction was engaged", (880.0, 1175.0)
    ),
    EarconDefinition(
        Earcon.READY, "the runtime became ready for normal operation",
        (660.0, 880.0, 1320.0),
    ),
    EarconDefinition(Earcon.WORK_STARTED, "autonomous work began", (523.0, 784.0)),
    EarconDefinition(
        Earcon.WORK_COMPLETED, "autonomous work completed", (784.0, 1047.0, 1319.0)
    ),
    EarconDefinition(
        Earcon.NEEDS_OPERATOR, "autonomous work began waiting for the operator",
        (740.0, 554.0, 740.0),
    ),
)
_DEFINITIONS = {definition.cue: definition for definition in EARCON_CATALOG}


class EarconAttemptStatus(StrEnum):
    PLAYED = "played"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EarconActivity:
    cue: Earcon
    status: EarconAttemptStatus
    observed_at: datetime
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class EarconSnapshot:
    output_available: bool
    catalog: tuple[EarconDefinition, ...]
    last_attempt: EarconActivity | None
    last_played: EarconActivity | None


class EarconOutput(Protocol):
    """Hardware adapter that plays one complete local WAV payload."""

    async def play_wav(self, wav: bytes) -> None: ...


class EarconPlayer:
    """Play named local cues under canonical speaker authority.

    Contention and output failures are intentionally contained here: signaling
    can never decide whether the semantic transition itself succeeds.
    """

    def __init__(self, resources: ResourceArbiter, output: EarconOutput | None) -> None:
        self._resources = resources
        self._output = output
        self._last_attempt: EarconActivity | None = None
        self._last_played: EarconActivity | None = None

    def snapshot(self) -> EarconSnapshot:
        """Return detached current-run evidence without touching audio resources."""
        return EarconSnapshot(
            self._output is not None, EARCON_CATALOG,
            self._last_attempt, self._last_played,
        )

    def _record(self, cue: Earcon, status: EarconAttemptStatus,
                reason: str | None = None) -> None:
        activity = EarconActivity(cue, status, datetime.now(UTC), reason)
        self._last_attempt = activity
        if status is EarconAttemptStatus.PLAYED:
            self._last_played = activity

    async def play(self, cue: Earcon | str) -> bool:
        cue = Earcon(cue)
        if self._output is None:
            self._record(cue, EarconAttemptStatus.SKIPPED, "output_unavailable")
            LOGGER.info("[EARCON] cue=%s status=skipped reason=disabled", cue.value)
            return False
        try:
            lease = self._resources.acquire(SPEAKER_RESOURCE, EARCON_SPEAKER_OWNER)
        except ResourceBusyError:
            self._record(cue, EarconAttemptStatus.SKIPPED, "speaker_busy")
            LOGGER.info("[EARCON] cue=%s status=skipped reason=speaker_busy", cue.value)
            return False
        try:
            await self._output.play_wav(earcon_wav(cue))
        except asyncio.CancelledError:
            self._record(cue, EarconAttemptStatus.FAILED, "cancelled")
            LOGGER.info("[EARCON] cue=%s status=failed error=CancelledError", cue.value)
            raise
        except Exception as error:
            self._record(cue, EarconAttemptStatus.FAILED, "output_error")
            LOGGER.warning(
                "[EARCON] cue=%s status=failed error=%s",
                cue.value, type(error).__name__,
            )
            return False
        else:
            self._record(cue, EarconAttemptStatus.PLAYED)
            LOGGER.info("[EARCON] cue=%s status=played", cue.value)
            return True
        finally:
            self._resources.release(lease)


async def _await_blocking(operation: Callable[[], _T]) -> _T:
    """Keep authority until executor playback exits, even on cancellation."""
    worker = asyncio.get_running_loop().run_in_executor(None, operation)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        raise


class FusionHatEarconOutput:
    """Local aplay adapter for the Fusion HAT+ speaker."""

    async def play_wav(self, wav: bytes) -> None:
        await _await_blocking(lambda: self._play_sync(wav))

    @staticmethod
    def _play_sync(wav: bytes) -> None:
        try:
            from fusion_hat.device import disable_speaker, enable_speaker
        except ImportError as error:
            raise RuntimeError("Fusion HAT speaker control is unavailable") from error
        enable_speaker()
        try:
            subprocess.run(["aplay", "--quiet"], input=wav, check=True)
        finally:
            disable_speaker()


def earcon_wav(cue: Earcon) -> bytes:
    """Generate one small deterministic PCM cue without media assets."""
    sample_rate, amplitude = 16_000, 7_000
    note_samples, gap_samples, ramp_samples = 1_600, 320, 160
    samples: list[int] = []
    notes = _DEFINITIONS[cue].notes
    for index, frequency in enumerate(notes):
        for position in range(note_samples):
            edge = min(position + 1, note_samples - position, ramp_samples)
            envelope = edge / ramp_samples
            samples.append(round(amplitude * envelope * math.sin(
                2.0 * math.pi * frequency * position / sample_rate
            )))
        if index + 1 < len(notes):
            samples.extend([0] * gap_samples)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return output.getvalue()
