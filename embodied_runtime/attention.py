"""Narrow goal-directed attention for reflex-driven body transitions."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging

from embodied_runtime.cognition import TextCognitionBackend
from embodied_runtime.events import BodyOrientationChanged, EventBus, Subscription

LOGGER = logging.getLogger(__name__)

INITIATIVE_REQUEST = (
    "Assess the attention stimulus against your current active goal. "
    "Briefly explain whether the current robot state appears relevant to that goal. "
    "This is a read-only attention pass; do not claim to have taken action."
)
MAX_DIAGNOSTIC_RESPONSE_CHARS = 2000


@dataclass(frozen=True, slots=True)
class AttentionStimulus:
    kind: str
    source: str
    previous_yaw_degrees: float
    previous_pitch_degrees: float
    yaw_degrees: float
    pitch_degrees: float

    def render(self) -> str:
        return "\n".join((
            "Attention stimulus",
            "This is a runtime-generated reason for an unsolicited read-only cognition",
            "pass. It is not operator input. Runtime context is authoritative for current",
            "facts; Active goal is authoritative for current intention; Working memory is",
            "historical and may be stale. No semantic tools or actions are available in this",
            "pass. Do not claim an action occurred unless Runtime context says it did.",
            "",
            f"  kind: {self.kind}",
            f"  source: {self.source}",
            f"  previous_yaw_deg: {self.previous_yaw_degrees}",
            f"  previous_pitch_deg: {self.previous_pitch_degrees}",
            f"  yaw_deg: {self.yaw_degrees}",
            f"  pitch_deg: {self.pitch_degrees}",
        ))


@dataclass(frozen=True, slots=True)
class AttentionStatus:
    enabled: bool
    state: str
    last_trigger: str | None
    last_source: str | None
    last_response: str | None


class GoalAttentionController:
    """Wake one read-only cognition request for a relevant semantic transition."""

    def __init__(self, *, enabled: bool, backend: TextCognitionBackend | None,
                 is_running: Callable[[], bool], has_active_goal: Callable[[], bool],
                 compose_instructions: Callable[[AttentionStimulus], str]) -> None:
        self.enabled = enabled
        self._backend = backend
        self._is_running = is_running
        self._has_active_goal = has_active_goal
        self._compose_instructions = compose_instructions
        self._subscription: Subscription[BodyOrientationChanged] | None = None
        self._task: asyncio.Task[None] | None = None
        self._state = "idle" if enabled else "disabled"
        self._last_trigger: str | None = None
        self._last_source: str | None = None
        self._last_response: str | None = None

    async def start(self, events: EventBus) -> None:
        if not self.enabled:
            return
        if self._subscription is not None:
            raise RuntimeError("Attention controller is already started")
        self._subscription = events.subscribe(BodyOrientationChanged, self._on_event)
        LOGGER.info("[ATTENTION] policy=goal_reflex_orientation status=ready")

    async def stop(self) -> None:
        subscription, self._subscription = self._subscription, None
        if subscription is not None:
            await subscription.close()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.enabled and self._state == "in_flight":
            self._state = "idle"

    def status(self) -> AttentionStatus:
        return AttentionStatus(self.enabled, self._state, self._last_trigger,
                               self._last_source, self._last_response)

    async def _on_event(self, event: BodyOrientationChanged) -> None:
        if not self._is_running() or self._backend is None or not self._has_active_goal():
            return
        if not event.source.startswith("reflex:"):
            return
        if self._task is not None and not self._task.done():
            LOGGER.info("[ATTENTION] event=body_orientation_changed decision=suppressed reason=in_flight")
            return
        stimulus = AttentionStimulus(
            "body_orientation_changed", event.source,
            event.previous_yaw_degrees, event.previous_pitch_degrees,
            event.yaw_degrees, event.pitch_degrees,
        )
        self._last_trigger = stimulus.kind
        self._last_source = stimulus.source
        self._last_response = None
        self._state = "in_flight"
        LOGGER.info("[ATTENTION] event=body_orientation_changed source=%s decision=wake", event.source)
        self._task = asyncio.create_task(self._reflect(stimulus), name="initiative:goal_attention")

    async def _reflect(self, stimulus: AttentionStimulus) -> None:
        backend = self._backend
        assert backend is not None
        LOGGER.info("[INITIATIVE] backend=%s request=started", backend.identifier)
        try:
            # Compose here, immediately before the provider call, so state, goal,
            # and bounded operator memory are all fresh.
            instructions = self._compose_instructions(stimulus)
            response = await backend.respond(
                INITIATIVE_REQUEST, instructions=instructions, tools=(),
                tool_executor=None, refreshed_instructions=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._state = "failed"
            LOGGER.warning("[INITIATIVE] backend=%s request=failed", backend.identifier)
            return
        self._last_response = response[:MAX_DIAGNOSTIC_RESPONSE_CHARS]
        self._state = "completed"
        LOGGER.info("[INITIATIVE] backend=%s request=completed response_chars=%s",
                    backend.identifier, len(response))
