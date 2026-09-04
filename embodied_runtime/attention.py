"""Narrow goal-directed attention for selected semantic transitions."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging

from embodied_runtime.events import (
    BodyOrientationChanged, Event, EventBus, MemoryPressureCleared,
    MemoryPressureRaised, Subscription, ThermalWarningCleared,
    ThermalWarningRaised,
)
from embodied_runtime.observations import (
    SemanticObservation, SemanticObservationFact,
    observation_from_body_orientation, observation_from_platform_transition,
)

LOGGER = logging.getLogger(__name__)

INITIATIVE_REQUEST = (
    "Assess the attention stimulus against your current active goal. "
    "Briefly explain whether the current robot state appears relevant to that goal. "
    "This is a read-only attention pass; do not claim to have taken action."
)
ACTION_INITIATIVE_REQUEST = (
    "Assess the attention stimulus against your current active goal. "
    "If one available semantic capability is appropriate and necessary, you may "
    "request at most one. Available capabilities are permissions, not obligations. "
    "If you address the operator, do not assume they will reply and do not wait for "
    "a response. "
    "Do not set, replace, resolve, or reinterpret the active goal."
)
CONTINUATION_INITIATIVE_REQUEST = (
    "Review the first autonomous effect and fresh current Runtime context against "
    "the SAME active goal. The first effect and its runtime-produced result are "
    "authoritative. If one remaining DISTINCT semantic capability is still necessary, "
    "you may request at most one; you are not required to act. Do not repeat the first "
    "capability or retry a rejected operation. Do not create, replace, reinterpret, "
    "resolve, or cancel goals. Do not assume another continuation will occur. After "
    "this request, autonomous semantic execution stops. Available capabilities are "
    "permissions, not obligations."
)
MAX_DIAGNOSTIC_RESPONSE_CHARS = 2000


@dataclass(frozen=True, slots=True, init=False)
class AttentionStimulus:
    observation: SemanticObservation

    def __init__(self, observation: SemanticObservation | str, *legacy: object) -> None:
        """Wrap an observation; temporarily accept the Phase 6 positional shape."""
        if isinstance(observation, SemanticObservation):
            if legacy:
                raise TypeError("unexpected arguments with SemanticObservation")
            value = observation
        else:
            if len(legacy) != 5:
                raise TypeError("legacy AttentionStimulus requires six arguments")
            source, previous_yaw, previous_pitch, yaw, pitch = legacy
            value = SemanticObservation(observation, str(source), (
                SemanticObservationFact("previous_yaw_deg", str(previous_yaw)),
                SemanticObservationFact("previous_pitch_deg", str(previous_pitch)),
                SemanticObservationFact("yaw_deg", str(yaw)),
                SemanticObservationFact("pitch_deg", str(pitch)),
            ))
        object.__setattr__(self, "observation", value)

    @property
    def kind(self) -> str:
        return self.observation.kind

    @property
    def source(self) -> str:
        return self.observation.source

    def render(self, *, actions_enabled: bool | None = False) -> str:
        capability_guidance = (
            (
                "Available tools are permissions, not obligations. Do not claim an action "
                "succeeded until its runtime-produced tool result says so."
            )
            if actions_enabled else
            (
                "No semantic tools or actions are available in this pass. Do not claim an "
                "action occurred unless Runtime context says it did."
            )
        )
        lines = [
            "Attention stimulus",
            "This is a runtime-generated reason for an unsolicited cognition",
            "pass. It is not operator input. Runtime context is authoritative for current",
            "facts; Active goal is authoritative for current intention; Working memory is",
            "historical and may be stale.",
        ]
        if actions_enabled is not None:
            lines.append(capability_guidance)
        lines.extend((
            "",
            "Semantic observation:",
            f"  kind: {self.kind}",
            f"  source: {self.source}",
        ))
        lines.extend(f"  {fact.name}: {fact.value}" for fact in self.observation.facts)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class InitiativeContinuationStimulus:
    """Provider-neutral facts grounding the sole continuation decision."""

    first_effect_name: str
    first_effect_status: str
    first_effect_result: str
    attention_kind: str
    attention_source: str

    def render(self) -> str:
        return "\n".join((
            "Initiative continuation stimulus",
            "The first effect already occurred; this runtime-produced result is authoritative.",
            f"  first_effect_name: {self.first_effect_name}",
            f"  first_effect_status: {self.first_effect_status}",
            f"  first_effect_result: {self.first_effect_result}",
            f"  attention_kind: {self.attention_kind}",
            f"  attention_source: {self.attention_source}",
        ))


@dataclass(frozen=True, slots=True)
class AttentionStatus:
    enabled: bool
    state: str
    last_trigger: str | None
    last_source: str | None
    last_response: str | None
    last_action: str | None
    last_action_status: str | None
    last_continuation_state: str
    last_continuation_action: str | None
    last_continuation_action_status: str | None
    last_continuation_response: str | None
    last_outcome_state: str
    last_goal_closure: str
    last_outcome_response: str | None


@dataclass(frozen=True, slots=True)
class InitiativeOutcome:
    response: str
    action: str | None = None
    action_status: str | None = None


class GoalAttentionController:
    """Select a relevant transition and own one initiative task's diagnostics."""

    def __init__(self, *, enabled: bool, platform_attention_enabled: bool = False,
                 backend_available: bool,
                 is_running: Callable[[], bool], has_active_goal: Callable[[], bool],
                 run_initiative: Callable[[AttentionStimulus], Awaitable[InitiativeOutcome]]) -> None:
        self.enabled = enabled
        self.platform_attention_enabled = platform_attention_enabled
        self._backend_available = backend_available
        self._is_running = is_running
        self._has_active_goal = has_active_goal
        self._run_initiative = run_initiative
        self._subscriptions: list[Subscription[Event]] = []
        self._task: asyncio.Task[None] | None = None
        self._state = "idle" if enabled else "disabled"
        self._last_trigger: str | None = None
        self._last_source: str | None = None
        self._last_response: str | None = None
        self._last_action: str | None = None
        self._last_action_status: str | None = None
        self._last_continuation_state = "not_run"
        self._last_continuation_action: str | None = None
        self._last_continuation_action_status: str | None = None
        self._last_continuation_response: str | None = None
        self._last_outcome_state = "not_run"
        self._last_goal_closure = "none"
        self._last_outcome_response: str | None = None

    async def start(self, events: EventBus) -> None:
        if not self.enabled:
            return
        if self._subscriptions:
            raise RuntimeError("Attention controller is already started")
        self._subscriptions.append(events.subscribe(BodyOrientationChanged, self._on_body_event))
        if self.platform_attention_enabled:
            for event_type in (
                ThermalWarningRaised, ThermalWarningCleared,
                MemoryPressureRaised, MemoryPressureCleared,
            ):
                self._subscriptions.append(events.subscribe(event_type, self._on_platform_event))
        LOGGER.info("[ATTENTION] policy=goal_semantic_transitions platform=%s status=ready",
                    "enabled" if self.platform_attention_enabled else "disabled")

    async def stop(self) -> None:
        subscriptions, self._subscriptions = tuple(self._subscriptions), []
        for subscription in subscriptions:
            await subscription.close()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.enabled and self._state == "in_flight":
            self._state = "idle"

    def status(self) -> AttentionStatus:
        return AttentionStatus(self.enabled, self._state, self._last_trigger,
                               self._last_source, self._last_response,
                               self._last_action, self._last_action_status,
                               self._last_continuation_state,
                               self._last_continuation_action,
                               self._last_continuation_action_status,
                               self._last_continuation_response,
                               self._last_outcome_state, self._last_goal_closure,
                               self._last_outcome_response)

    def record_action(self, action: str, status: str) -> None:
        """Record the latest runtime-produced result for the in-flight episode."""
        self._last_action = action
        self._last_action_status = status

    def record_outcome(
        self, *, state: str | None = None, closure: str | None = None,
        response: str | None = None,
    ) -> None:
        """Update volatile outcome diagnostics without interpreting goal semantics."""
        if state is not None:
            self._last_outcome_state = state
        if closure is not None:
            self._last_goal_closure = closure
        if response is not None:
            self._last_outcome_response = response[:MAX_DIAGNOSTIC_RESPONSE_CHARS]

    def record_continuation(
        self, *, state: str | None = None, action: str | None = None,
        action_status: str | None = None, response: str | None = None,
    ) -> None:
        """Update volatile continuation diagnostics without owning execution."""
        if state is not None:
            self._last_continuation_state = state
        if action is not None:
            self._last_continuation_action = action
        if action_status is not None:
            self._last_continuation_action_status = action_status
        if response is not None:
            self._last_continuation_response = response[:MAX_DIAGNOSTIC_RESPONSE_CHARS]

    async def _on_body_event(self, event: BodyOrientationChanged) -> None:
        if not event.source.startswith("reflex:"):
            return
        await self._consider(observation_from_body_orientation(event))

    async def _on_platform_event(self, event: Event) -> None:
        assert isinstance(event, (ThermalWarningRaised, ThermalWarningCleared,
                                  MemoryPressureRaised, MemoryPressureCleared))
        if event.source != "platform_monitor":
            return
        await self._consider(observation_from_platform_transition(event))

    async def _consider(self, observation: SemanticObservation) -> None:
        if not self._is_running() or not self._backend_available or not self._has_active_goal():
            return
        if self._task is not None and not self._task.done():
            LOGGER.info("[ATTENTION] event=%s decision=suppressed reason=in_flight",
                        observation.kind)
            return
        stimulus = AttentionStimulus(observation)
        self._last_trigger = stimulus.kind
        self._last_source = stimulus.source
        self._last_response = None
        self._last_action = None
        self._last_action_status = None
        self._last_continuation_state = "not_run"
        self._last_continuation_action = None
        self._last_continuation_action_status = None
        self._last_continuation_response = None
        self._last_outcome_state = "not_run"
        self._last_goal_closure = "none"
        self._last_outcome_response = None
        self._state = "in_flight"
        LOGGER.info("[ATTENTION] event=%s source=%s decision=wake",
                    stimulus.kind, stimulus.source)
        self._task = asyncio.create_task(self._reflect(stimulus), name="initiative:goal_attention")

    async def _reflect(self, stimulus: AttentionStimulus) -> None:
        try:
            outcome = await self._run_initiative(stimulus)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._state = "failed"
            return
        self._last_response = outcome.response[:MAX_DIAGNOSTIC_RESPONSE_CHARS]
        self._last_action = outcome.action
        self._last_action_status = outcome.action_status
        self._state = "completed"
