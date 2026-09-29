"""Deterministic, runtime-owned semantic audio cues."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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

    async def play(self, cue: Earcon | str) -> bool:
        cue = Earcon(cue)
        if self._output is None:
            LOGGER.info("[EARCON] cue=%s status=skipped reason=disabled", cue.value)
            return False
        try:
            lease = self._resources.acquire(SPEAKER_RESOURCE, EARCON_SPEAKER_OWNER)
        except ResourceBusyError:
            LOGGER.info("[EARCON] cue=%s status=skipped reason=speaker_busy", cue.value)
            return False
        try:
            await self._output.play_wav(earcon_wav(cue))
        except asyncio.CancelledError:
            LOGGER.info("[EARCON] cue=%s status=failed error=CancelledError", cue.value)
            raise
        except Exception as error:
            LOGGER.warning(
                "[EARCON] cue=%s status=failed error=%s",
                cue.value, type(error).__name__,
            )
            return False
        else:
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


_NOTES = {
    Earcon.ENGAGEMENT: (880.0, 1175.0),
    Earcon.READY: (660.0, 880.0, 1320.0),
    Earcon.WORK_STARTED: (523.0, 784.0),
    Earcon.WORK_COMPLETED: (784.0, 1047.0, 1319.0),
    Earcon.NEEDS_OPERATOR: (740.0, 554.0, 740.0),
}


def earcon_wav(cue: Earcon) -> bytes:
    """Generate one small deterministic PCM cue without media assets."""
    sample_rate, amplitude = 16_000, 7_000
    note_samples, gap_samples, ramp_samples = 1_600, 320, 160
    samples: list[int] = []
    for index, frequency in enumerate(_NOTES[cue]):
        for position in range(note_samples):
            edge = min(position + 1, note_samples - position, ramp_samples)
            envelope = edge / ramp_samples
            samples.append(round(amplitude * envelope * math.sin(
                2.0 * math.pi * frequency * position / sample_rate
            )))
        if index + 1 < len(_NOTES[cue]):
            samples.extend([0] * gap_samples)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return output.getvalue()
