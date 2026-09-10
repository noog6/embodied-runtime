"""Application lifecycle orchestration."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
import json
import logging
import math
import unicodedata
from time import monotonic
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from embodied_runtime.body.base import BodyBackend
from embodied_runtime.attention import (
    ACTION_INITIATIVE_REQUEST, CONTINUATION_INITIATIVE_REQUEST, INITIATIVE_REQUEST,
    AcquisitionFollowupStimulus, AttentionEpisode, AttentionStimulus,
    AttentionEpisodeCoordinator, GoalAttentionController,
    EpisodeCompletionReason, InitiativeContinuationStimulus, InitiativeOutcome,
    MAX_AUTONOMOUS_ACQUISITIONS_PER_EPISODE,
    concern_for_stimulus,
)
from embodied_runtime.cognition import (
    ActiveGoal,
    CognitionContext,
    CognitionToolCall,
    CognitionToolDefinition,
    CognitionToolResult,
    GoalOutcomeStimulus,
    InitiativeAcquisitionOutcome,
    InitiativeEffectOutcome,
    TextCognitionBackend,
    WorkingMemory,
    WorkingMemoryToolOutcome,
    compose_cognition_instructions,
    validate_goal_description,
)
from embodied_runtime.events import (
    ApplicationStarted,
    BodyOrientationChanged,
    EventBus,
    PresenceChanged,
)
from embodied_runtime.hardware.base import HardwareBackend
from embodied_runtime.interaction import (
    MAX_OPERATOR_MESSAGE_CHARS, OperatorMessage, OperatorMessageSink,
)
from embodied_runtime.inspection import (
    HostSelfInspector, SELF_INSPECTION_AREAS, SelfInspectionFact,
    SelfInspectionResult, SelfInspector,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.perception import (
    MAX_CAMERA_FRAME_BYTES, VisualPerceptionBackend, VisualPerceptionResult,
)
from embodied_runtime.reflexes import Reflex
from embodied_runtime.sensing.camera import CameraBackend, CameraFrame
from embodied_runtime.platform import (
    HostPlatformProvider,
    PlatformMonitor,
    PlatformMonitorPolicy,
    PlatformProvider,
    PlatformSnapshot,
)
from embodied_runtime.state import (
    BodyState, LifecycleState, PowerState, PresenceState, RuntimeState,
)
from embodied_runtime.temporal import TemporalFollowupController, TemporalFollowupStatus
from embodied_runtime.temporal_context import TemporalContext, TemporalSituation
from embodied_runtime.voice import (
    TextToSpeechProvider,
    VoiceInteraction,
    VoiceProvider,
    VoiceSessionPolicy,
)

LOGGER = logging.getLogger(__name__)
OPERATOR_SOURCE: ContextVar[str] = ContextVar("operator_source", default="operator")

ORIENT_BODY_TOOL = CognitionToolDefinition(
    name="orient_body",
    description="Request an absolute semantic body orientation using numeric degrees.",
    parameters={
        "type": "object",
        "properties": {
            "yaw_degrees": {"type": "number"},
            "pitch_degrees": {"type": "number"},
        },
        "required": ["yaw_degrees", "pitch_degrees"],
        "additionalProperties": False,
    },
)

ADDRESS_OPERATOR_TOOL = CognitionToolDefinition(
    name="address_operator",
    description=(
        "Send one short plain-text statement or question to the operator. A question "
        "does not wait for a reply. An applied result means the configured operator "
        "channel accepted the message, not that the human read or acknowledged it. "
        "Available capabilities are permissions, not obligations."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {"type": "string", "maxLength": MAX_OPERATOR_MESSAGE_CHARS}
        },
        "required": ["message"],
        "additionalProperties": False,
    },
)

SET_GOAL_TOOL = CognitionToolDefinition(
    name="set_goal",
    description=(
        "Establish one ongoing goal only when the operator clearly asks "
        "to adopt or retain an objective. Setting it does not perform it."
    ),
    parameters={
        "type": "object",
        "properties": {"description": {"type": "string", "maxLength": 500}},
        "required": ["description"],
        "additionalProperties": False,
    },
)

RESOLVE_GOAL_TOOL = CognitionToolDefinition(
    name="resolve_goal",
    description="Resolve the current active goal as completed or cancelled.",
    parameters={
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["completed", "cancelled"]}
        },
        "required": ["outcome"],
        "additionalProperties": False,
    },
)

COMPLETE_GOAL_TOOL = CognitionToolDefinition(
    name="complete_goal",
    description=(
        "Complete the same active goal only when the action outcome makes that "
        "goal terminally complete. Current satisfaction alone is insufficient."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
)

INSPECT_SELF_TOOL = CognitionToolDefinition(
    name="inspect_self",
    description=(
        "Read one bounded runtime-owned local condition. Use only when a missing "
        "local fact is materially relevant. This is read-only and is not an effect."
    ),
    parameters={
        "type": "object",
        "properties": {"area": {"type": "string", "enum": list(SELF_INSPECTION_AREAS)}},
        "required": ["area"],
        "additionalProperties": False,
    },
)

OBSERVE_SCENE_TOOL = CognitionToolDefinition(
    name="observe_scene",
    description=(
        "Capture exactly one current camera frame and obtain one bounded, "
        "model-generated visual interpretation. This is read-only and not an effect."
    ),
    parameters={
        "type": "object",
        "properties": {"focus": {
            "type": "string", "minLength": 1, "maxLength": 300,
        }},
        "required": ["focus"],
        "additionalProperties": False,
    },
)

SCHEDULE_FOLLOWUP_TOOL = CognitionToolDefinition(
    name="schedule_followup",
    description=(
        "Create the runtime's one session-local, one-shot relative follow-up. "
        "It is bound to the current active goal and creates fresh attention when due; "
        "it does not reserve future authority. This is a semantic effect."
    ),
    parameters={
        "type": "object",
        "properties": {
            "delay_seconds": {"type": "integer", "minimum": 10, "maximum": 86400},
            "purpose": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "required": ["delay_seconds", "purpose"],
        "additionalProperties": False,
    },
)

ACQUISITION_FOLLOWUP_REQUEST = (
    "Review the ordered acquisition evidence against freshly reconstructed Runtime "
    "context and the SAME episode concern and active goal. Follow the explicit remaining "
    "acquisition budget in the stimulus. Request at most one offered capability, or none."
)
OPERATOR_EPISODE_CONCERN = "Respond to operator utterance"
# Compatibility names for callers that identified the Phase 16.1 request by
# acquisition type. Both now use the cumulative Phase 16.2 grammar.
INSPECTION_FOLLOWUP_REQUEST = ACQUISITION_FOLLOWUP_REQUEST
VISUAL_FOLLOWUP_REQUEST = ACQUISITION_FOLLOWUP_REQUEST

OUTCOME_EVALUATION_REQUEST = (
    "Evaluate the bounded autonomous effect sequence against the current active goal. "
    "Current Runtime context is authoritative for what is true now. Active goal is "
    "authoritative for current intention. Working memory is historical and may be "
    "stale. The outcome stimulus describes one or two runtime-produced effect results. "
    "Determine whether the SAME active goal is terminally complete. Do not complete "
    "an ongoing or maintenance goal merely because it is currently satisfied. If "
    "the goal is terminally complete and the runtime provides a completion capability, "
    "you may request it. Otherwise leave the goal active. Do not create, replace, "
    "reinterpret, or cancel goals. Do not request another body action."
)


@dataclass(frozen=True)
class ApplicationOptions:
    startup_prompt: str | None = None
    initiative_enabled: bool = False
    initiative_platform_attention_enabled: bool = False
    initiative_actions_enabled: bool = False
    initiative_messages_enabled: bool = False
    initiative_continuation_enabled: bool = False
    initiative_goal_closure_enabled: bool = False


@dataclass(frozen=True)
class RuntimeSummary:
    profile_id: str
    profile_name: str
    hardware_backend: str
    hardware_is_physical: bool
    capabilities: tuple[str, ...]
    startup_prompt_provided: bool
    lifecycle_status: LifecycleState


@dataclass(frozen=True)
class BodySummary:
    backend: str
    is_physical: bool
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class CameraSummary:
    backend: str
    is_physical: bool
    is_running: bool


class RobotApplication:
    def __init__(
        self,
        profile: RobotProfile,
        hardware: HardwareBackend,
        options: ApplicationOptions | None = None,
        events: EventBus | None = None,
        platform_provider: PlatformProvider | None = None,
        platform_monitor_policy: PlatformMonitorPolicy | None = None,
        body_backend: BodyBackend | None = None,
        reflexes: Sequence[Reflex] = (),
        camera_backend: CameraBackend | None = None,
        cognition_backend: TextCognitionBackend | None = None,
        working_memory: WorkingMemory | None = None,
        operator_message_sink: OperatorMessageSink | None = None,
        self_inspector: SelfInspector | None = None,
        visual_perception_backend: VisualPerceptionBackend | None = None,
        temporal_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic_clock: Callable[[], float] | None = None,
        voice_provider: VoiceProvider | None = None,
        text_to_speech_provider: TextToSpeechProvider | None = None,
        voice_policy: VoiceSessionPolicy = VoiceSessionPolicy(),
        voice_wake_words: list[str] | None = None,
        timezone_name: str = "UTC",
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.profile = profile
        self.hardware = hardware
        self._timezone_name = timezone_name
        self._timezone = ZoneInfo(timezone_name)
        self._wall_clock = wall_clock
        self.options = options or ApplicationOptions()
        self.events = events or EventBus()
        self.body_backend = body_backend
        self.camera_backend = camera_backend
        self._cognition_backend = cognition_backend
        self._active_operator_cognition_task: asyncio.Task[object] | None = None
        self._operator_message_sink = operator_message_sink
        self._self_inspector = self_inspector or HostSelfInspector()
        self._visual_perception_backend = visual_perception_backend
        self.working_memory = (
            working_memory if working_memory is not None else WorkingMemory()
        )
        self.voice = VoiceInteraction(
            voice_provider,
            text_to_speech_provider,
            lambda text: self.handle_operator_utterance(text, source="voice"),
            voice_policy,
            wake_words=voice_wake_words,
        )
        self._active_goal: ActiveGoal | None = None
        self._monotonic = monotonic_clock or monotonic
        self._active_goal_started_monotonic: tuple[ActiveGoal, float] | None = None
        self._last_operator_turn_completed_monotonic: float | None = None
        self._next_goal_id = 1
        self._reflexes = tuple(reflexes)
        self._started_reflexes: list[Reflex] = []
        self._runtime_state = RuntimeState(LifecycleState.CREATED)
        self._platform_provider = platform_provider or HostPlatformProvider()
        self._stop_requested = asyncio.Event()
        self._platform_monitor = PlatformMonitor(
            self._platform_provider,
            self.events,
            self._replace_platform_state,
            lambda: self.state is LifecycleState.RUNNING,
            policy=platform_monitor_policy,
        )
        self.temporal = TemporalFollowupController(
            self.events, is_running=lambda: self.state is LifecycleState.RUNNING,
            current_goal=lambda: self._active_goal, sleep=temporal_sleep,
            monotonic_clock=self._monotonic,
        )
        self.episode_coordinator = AttentionEpisodeCoordinator(self._monotonic)
        self.attention = GoalAttentionController(
            enabled=self.options.initiative_enabled,
            platform_attention_enabled=self.options.initiative_platform_attention_enabled,
            backend_available=self._cognition_backend is not None,
            is_running=lambda: self.state is LifecycleState.RUNNING,
            has_active_goal=lambda: self._active_goal is not None,
            current_goal=lambda: self._active_goal,
            claim_temporal_due=self.temporal.claim_due,
            coordinator=self.episode_coordinator,
            run_initiative=self._request_initiative,
        )

    @property
    def runtime_state(self) -> RuntimeState:
        return self._runtime_state

    @property
    def state(self) -> LifecycleState:
        """Compatibility view of the authoritative lifecycle state."""
        return self._runtime_state.lifecycle

    @property
    def active_goal(self) -> ActiveGoal | None:
        return self._active_goal

    def temporal_context(self) -> TemporalContext:
        """Build fresh local wall-clock grounding for one cognition boundary."""
        instant = self._wall_clock()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("wall clock must return an offset-aware datetime")
        return TemporalContext(
            instant.astimezone(self._timezone), self._timezone_name
        )

    def temporal_situation(self) -> TemporalSituation:
        """Build fresh elapsed-time grounding from the shared monotonic clock."""
        now = self._monotonic()
        goal = self._active_goal
        marker = self._active_goal_started_monotonic
        goal_age = None
        if goal is not None and marker is not None and marker[0] is goal:
            goal_age = int(max(0.0, now - marker[1]))
        status = self.temporal.status()
        last = self.episode_coordinator.last
        completed = self.episode_coordinator.last_completed_monotonic
        return TemporalSituation(
            goal.id if goal_age is not None else None, goal_age,
            status.state, status.remaining_seconds, status.purpose,
            None if self._last_operator_turn_completed_monotonic is None else
            int(max(0.0, now - self._last_operator_turn_completed_monotonic)),
            None if last is None or completed is None else last.id,
            None if last is None or completed is None else int(max(0.0, now - completed)),
        )

    def set_goal(self, description: object) -> ActiveGoal:
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Setting a goal requires a running application")
        normalized = validate_goal_description(description)
        if self._active_goal is not None:
            raise RuntimeError("an active goal already exists")
        goal = ActiveGoal(self._next_goal_id, normalized)
        self._next_goal_id += 1
        self._active_goal = goal
        self._active_goal_started_monotonic = (goal, self._monotonic())
        LOGGER.info("[GOAL] goal=G%s status=active chars=%s", goal.id, len(normalized))
        return goal

    def resolve_goal(self, outcome: object) -> ActiveGoal:
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Resolving a goal requires a running application")
        if outcome not in ("completed", "cancelled") or not isinstance(outcome, str):
            raise ValueError("outcome must be completed or cancelled")
        if self._active_goal is None:
            raise RuntimeError("no active goal exists")
        previous = self._active_goal
        self._active_goal = None
        self._active_goal_started_monotonic = None
        self.temporal.cancel("goal_changed")
        LOGGER.info("[GOAL] status=%s", outcome)
        return previous

    def clear_goal(self) -> bool:
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Clearing a goal requires a running application")
        cleared = self._active_goal is not None
        self._active_goal = None
        self._active_goal_started_monotonic = None
        if cleared:
            self.temporal.cancel("goal_changed")
            LOGGER.info("[GOAL] status=cleared")
        return cleared

    def _set_lifecycle(self, lifecycle: LifecycleState) -> None:
        self._runtime_state = replace(self._runtime_state, lifecycle=lifecycle)

    def refresh_platform_state(self) -> PlatformSnapshot:
        snapshot = self._platform_provider.snapshot()
        self._replace_platform_state(snapshot)
        return snapshot

    def _replace_platform_state(self, snapshot: PlatformSnapshot) -> None:
        self._runtime_state = replace(self._runtime_state, platform=snapshot)

    def refresh_power_state(self) -> PowerState:
        """Refresh backend-neutral authoritative power state on demand."""
        power = PowerState(battery_voltage_v=None)
        if "battery_voltage" in self.hardware.capabilities:
            try:
                power = PowerState(
                    battery_voltage_v=self.hardware.read_battery_voltage_v()
                )
            except Exception:
                self._runtime_state = replace(self._runtime_state, power=power)
                raise
        if power != self._runtime_state.power:
            self._runtime_state = replace(self._runtime_state, power=power)
        return power

    async def start(self) -> None:
        if self.state is not LifecycleState.CREATED:
            raise RuntimeError(f"Cannot start application in {self.state} state")
        self._set_lifecycle(LifecycleState.STARTING)
        LOGGER.info(
            "[APP] starting profile=%s hardware=%s",
            self.profile.identifier,
            self.hardware.identifier,
        )
        platform_state = self.refresh_platform_state()
        self._platform_monitor.establish_baseline(platform_state)
        LOGGER.info(
            "[PLATFORM] hostname=%s system=%s machine=%s python=%s status=ready",
            platform_state.hostname,
            platform_state.system,
            platform_state.machine,
            platform_state.python_version,
        )
        await self.events.start()
        hardware_started = False
        camera_start_attempted = False
        try:
            self.hardware.start()
            hardware_started = True
            self.refresh_power_state()
            LOGGER.info(
                "[HW] backend=%s physical=%s status=ready",
                self.hardware.identifier,
                str(self.hardware.is_physical).lower(),
            )
            if self.body_backend is not None:
                body_state = await self.body_backend.start()
                self._runtime_state = replace(self._runtime_state, body=body_state)
                LOGGER.info(
                    "[BODY] backend=%s physical=%s capabilities=%s status=ready",
                    self.body_backend.identifier,
                    str(self.body_backend.is_physical).lower(),
                    ",".join(self.body_backend.capabilities) or "none",
                )
            if self.camera_backend is not None:
                # Offer stop even when start fails so injected implementations can
                # release resources acquired during partial initialization.
                camera_start_attempted = True
                self.camera_backend.start()
                LOGGER.info(
                    "[CAMERA] backend=%s physical=%s status=ready",
                    self.camera_backend.identifier,
                    str(self.camera_backend.is_physical).lower(),
                )
            for reflex in self._reflexes:
                # Record before starting so a partially established subscription
                # is still offered cleanup if start raises.
                self._started_reflexes.append(reflex)
                await reflex.start(self.events, self)
        except BaseException:
            await self._stop_reflexes_for_cleanup()
            if camera_start_attempted:
                try:
                    self.camera_backend.stop()
                except BaseException:
                    LOGGER.exception("[CAMERA] cleanup_failed")
            if self.body_backend is not None:
                try:
                    await self.body_backend.stop()
                except BaseException:
                    LOGGER.exception("[BODY] cleanup_failed")
            if hardware_started:
                try:
                    self.hardware.stop()
                except BaseException:
                    LOGGER.exception("[HW] cleanup_failed")
            self._set_lifecycle(LifecycleState.STOPPED)
            try:
                await self.events.stop()
            except BaseException:
                LOGGER.exception("[EVENT] cleanup_failed")
            raise
        self._set_lifecycle(LifecycleState.RUNNING)
        try:
            await self.attention.start(self.events)
        except BaseException:
            await self.stop()
            raise
        LOGGER.info("[APP] running profile=%s", self.profile.identifier)
        await self.events.publish(ApplicationStarted(source="application"))
        self._platform_monitor.start()
        self.voice.start_wake_listener()
        LOGGER.info(
            "[PULSE] monitor=platform interval_s=%s heartbeat_s=%s status=ready",
            str(self._platform_monitor.policy.interval_seconds),
            "off" if self._platform_monitor.policy.heartbeat_interval_seconds is None
            else str(self._platform_monitor.policy.heartbeat_interval_seconds),
        )

    async def stop(self) -> None:
        if self.state is LifecycleState.STOPPED:
            return
        self._set_lifecycle(LifecycleState.STOPPING)
        LOGGER.info("[APP] stopping")
        failure: BaseException | None = None
        try:
            await self.voice.stop()
        except BaseException as error:
            failure = error
        try:
            await self._stop_operator_cognition()
        except BaseException as error:
            failure = failure or error
        try:
            await self.temporal.stop()
        except BaseException as error:
            failure = error
        try:
            await self.attention.stop()
        except BaseException as error:
            failure = error
        try:
            await self._platform_monitor.stop()
        except BaseException as error:
            failure = error
        try:
            await self._stop_reflexes()
        except BaseException as error:
            failure = failure or error
        if self.camera_backend is not None:
            try:
                self.camera_backend.stop()
            except BaseException as error:
                LOGGER.exception("[CAMERA] stop_failed")
                failure = failure or error
        if self.body_backend is not None:
            try:
                await self.body_backend.stop()
            except BaseException as error:
                LOGGER.exception("[BODY] stop_failed")
                failure = failure or error
        try:
            self.hardware.stop()
        except BaseException as error:
            failure = failure or error
        try:
            self._set_lifecycle(LifecycleState.STOPPED)
            self._stop_requested.set()
            await self.events.stop()
            LOGGER.info("[APP] stopped")
        finally:
            if failure is not None:
                raise failure

    async def run(self) -> None:
        await self.start()
        try:
            await self._stop_requested.wait()
        except asyncio.CancelledError:
            LOGGER.info("[APP] interrupted")
            raise
        except KeyboardInterrupt:
            LOGGER.info("[APP] interrupted")
        finally:
            await self.stop()

    def request_stop(self) -> None:
        """Request an orderly stop from code running on the application loop."""
        self._stop_requested.set()

    def capture_camera_frame(self) -> CameraFrame:
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Camera capture requires a running application")
        if self.camera_backend is None:
            raise RuntimeError("No camera backend is configured")
        frame = self.camera_backend.capture_frame()
        LOGGER.info(
            "[CAMERA] capture width=%s height=%s media_type=%s bytes=%s",
            frame.width, frame.height, frame.media_type, len(frame.data),
        )
        return frame

    async def request_cognition(self, message: str, *, source: str | None = None) -> str:
        """Run one finite operator attention episode."""
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Cognition requires a running application")
        if self._cognition_backend is None:
            raise RuntimeError("No cognition backend is configured")
        if not message or not message.strip():
            raise ValueError("Cognition message must be non-empty")
        backend = self._cognition_backend
        episode = await self.episode_coordinator.start_operator(
            source or OPERATOR_SOURCE.get(), OPERATOR_EPISODE_CONCERN
        )
        if self.state is not LifecycleState.RUNNING:
            await self._finish_operator_episode(episode, "cancelled")
            raise RuntimeError("Cognition requires a running application")
        task = asyncio.current_task()
        if task is None:
            await self._finish_operator_episode(episode, "cancelled")
            raise RuntimeError("Cognition requires an application task")
        self._active_operator_cognition_task = task
        try:
            return await self._run_operator_episode(message, backend, episode)
        finally:
            if self._active_operator_cognition_task is task:
                self._active_operator_cognition_task = None

    async def _run_operator_episode(
        self, message: str, backend: TextCognitionBackend,
        episode: AttentionEpisode,
    ) -> str:
        """Execute the already-acquired finite operator episode."""
        prior_memory = self.working_memory.snapshot()
        tool_outcomes: list[WorkingMemoryToolOutcome] = []
        acquisitions: list[InitiativeAcquisitionOutcome] = []
        acquisition_requests: dict[tuple[str, str], CognitionToolResult] = {}
        try:
            # Explicitly bounded grammar: initial decision, then at most two
            # post-acquisition decisions. A non-acquisition decision terminates.
            response = ""
            for stage in range(3):
                if self.state is not LifecycleState.RUNNING:
                    raise asyncio.CancelledError
                acquired = False
                capability_consumed = False
                grounded_goal = self._active_goal
                tools = self._operator_episode_tools(len(acquisitions))

                async def execute_tool(call: CognitionToolCall) -> CognitionToolResult:
                    nonlocal acquired, capability_consumed
                    if self.state is not LifecycleState.RUNNING:
                        raise asyncio.CancelledError
                    if capability_consumed:
                        return self._rejected_tool(
                            call.name, "operator capability request already consumed"
                        )
                    capability_consumed = True
                    if not any(tool.name == call.name for tool in tools):
                        result = self._rejected_tool(call.name, "tool is not available")
                        tool_outcomes.append(
                            WorkingMemoryToolOutcome(call.name, result.output)
                        )
                        return result
                    if call.name in (INSPECT_SELF_TOOL.name, OBSERVE_SCENE_TOOL.name):
                        number = len(acquisitions) + 1
                        LOGGER.info(
                            "[ATTENTION] episode=E%s acquisition=%s/2 tool=%s status=requested",
                            episode.id, number, call.name,
                        )
                        acquisition_key = (call.name, call.arguments)
                        if acquisition_key in acquisition_requests:
                            # Re-present already accumulated evidence without
                            # repeating I/O or consuming another acquisition.
                            result = acquisition_requests[acquisition_key]
                            return result
                        elif call.name == INSPECT_SELF_TOOL.name:
                            result, inspection = self._execute_self_inspection(call)
                            perception = None
                        else:
                            result, perception = await self._execute_visual_perception(call)
                            inspection = None
                        acquisition_requests[acquisition_key] = result
                        try:
                            status = json.loads(result.output).get("status", "rejected")
                        except (json.JSONDecodeError, AttributeError):
                            status = "rejected"
                        acquisitions.append(InitiativeAcquisitionOutcome(
                            call.name, status, result.output,
                            inspection_result=inspection,
                            perception_result=perception,
                        ))
                        acquired = True
                        LOGGER.info(
                            "[ATTENTION] episode=E%s acquisition=%s/2 tool=%s status=%s",
                            episode.id, number, call.name, status,
                        )
                    else:
                        result = await self._execute_cognition_tool(
                            call, expected_goal=grounded_goal
                        )
                    tool_outcomes.append(WorkingMemoryToolOutcome(call.name, result.output))
                    return result

                LOGGER.info("[COGNITION] backend=%s request=started", backend.identifier)
                response = await backend.respond(
                    message,
                    instructions=self._operator_episode_instructions(
                        episode, message, prior_memory, acquisitions
                    ),
                    tools=tools,
                    tool_executor=execute_tool if tools else None,
                    refreshed_instructions=lambda: self._operator_episode_instructions(
                        episode, message, prior_memory, acquisitions
                    ),
                )
                LOGGER.info(
                    "[COGNITION] backend=%s request=completed response_chars=%s",
                    backend.identifier, len(response),
                )
                if not acquired:
                    break
        except asyncio.CancelledError:
            await self._finish_operator_episode(episode, "cancelled")
            raise
        except Exception:
            LOGGER.warning("[COGNITION] backend=%s request=failed", backend.identifier)
            await self._finish_operator_episode(episode, "error")
            raise
        if self.state is not LifecycleState.RUNNING:
            await self._finish_operator_episode(episode, "cancelled")
            raise asyncio.CancelledError
        self.working_memory.append(message, response, tool_outcomes)
        self._last_operator_turn_completed_monotonic = self._monotonic()
        await self._finish_operator_episode(episode, "handled")
        return response

    async def _stop_operator_cognition(self) -> None:
        """Cancel and join the sole active operator cognition during shutdown."""
        task = self._active_operator_cognition_task
        if task is None or task.done():
            return
        if task is asyncio.current_task():
            raise RuntimeError("operator cognition cannot await its own shutdown")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _finish_operator_episode(
        self, episode: AttentionEpisode, reason: EpisodeCompletionReason,
    ) -> None:
        """Close operator attention and offer due work only while still running."""
        self.episode_coordinator.close(episode, reason)
        if self.state is LifecycleState.RUNNING:
            await self.attention.release_temporal_due()

    async def handle_operator_utterance(
        self, message: str, *, source: str = "operator"
    ) -> str:
        """Route one typed or spoken operator utterance through cognition."""
        token = OPERATOR_SOURCE.set(source)
        try:
            return await self.request_cognition(message)
        finally:
            OPERATOR_SOURCE.reset(token)

    def _operator_episode_tools(self, acquisitions_used: int) -> tuple[CognitionToolDefinition, ...]:
        tools = self.cognition_tools()
        if acquisitions_used >= 2:
            return tuple(tool for tool in tools if tool.name not in (
                INSPECT_SELF_TOOL.name, OBSERVE_SCENE_TOOL.name
            ))
        return tools

    def _operator_episode_instructions(
        self, episode: AttentionEpisode, message: str, working_memory,
        acquisitions: list[InitiativeAcquisitionOutcome],
    ) -> str:
        remaining = 2 - len(acquisitions)
        lines = [
            compose_cognition_instructions(
                self.cognition_context(), self.temporal_context(), self.temporal_situation(),
                self.options.startup_prompt,
                working_memory, self._active_goal,
            ),
            episode.render(),
            "Operator episode policy",
            "The original operator request is the current request; do not copy it into episode identity.",
            f"  acquisitions_used: {len(acquisitions)}",
            f"  acquisitions_remaining: {remaining}",
            "At most one offered capability may be requested in this cognition stage.",
        ]
        if acquisitions:
            lines.append("Ordered acquisition evidence:")
            for index, acquisition in enumerate(acquisitions, 1):
                lines.extend(acquisition.render(index))
        if remaining == 0:
            lines.append("No further read-only acquisition is available.")
        return "\n\n".join(lines)

    def _cognition_instructions(self, working_memory=None) -> str:
        if working_memory is None:
            working_memory = self.working_memory.snapshot()
        return compose_cognition_instructions(
            self.cognition_context(), self.temporal_context(), self.temporal_situation(),
            self.options.startup_prompt, working_memory,
            self._active_goal,
        )

    def _attention_instructions(
        self, stimulus: AttentionStimulus, episode: AttentionEpisode, working_memory, *, capabilities_available: bool,
        expected_goal: ActiveGoal | None = None,
    ) -> str:
        context = compose_cognition_instructions(
            self.cognition_context(), self.temporal_context(), self.temporal_situation(),
            self.options.startup_prompt, working_memory,
            expected_goal if self._active_goal is expected_goal else None,
        )
        sequencing = (
            "\n\nYou may request at most one semantic capability in this request. "
            "If the active goal clearly requires two distinct semantic effects in order, "
            "choose only the effect that should happen FIRST. After a successful first "
            "effect, the runtime may provide one bounded continuation opportunity."
            if self.options.initiative_continuation_enabled and capabilities_available
            else ""
        )
        inspection_guidance = (
            "\n\nRead-only inspect_self and observe_scene capabilities may be available "
            "for bounded missing information. This episode permits at most two acquisition "
            "attempts across separate cognition requests. Use one only when materially "
            "relevant to the active goal; do not acquire information merely because "
            "a capability exists, retry an acquisition, or request the same information. "
            "Every acquisition must serve this SAME concern and goal. You may request at "
            "most one capability in this request."
            if capabilities_available else ""
        )
        return (
            f"{context}\n\n{episode.render()}\n\n{stimulus.render(actions_enabled=capabilities_available)}"
            f"{inspection_guidance}{sequencing}"
        )

    async def _request_initiative(
        self, stimulus: AttentionStimulus, episode: AttentionEpisode | None = None,
    ) -> InitiativeOutcome:
        backend = self._cognition_backend
        if backend is None:
            raise RuntimeError("No cognition backend is configured")
        prior_memory = self.working_memory.snapshot()
        expected_goal = self._active_goal
        # Private-call compatibility for focused executor tests. Accepted runtime
        # attention always supplies the controller-allocated positive episode ID.
        if episode is None and expected_goal is not None:
            episode = AttentionEpisode(
                0, stimulus.kind, stimulus.source, concern_for_stimulus(stimulus),
                expected_goal.id, state="active",
            )
        if episode is None:
            raise RuntimeError("attention initiative requires an active goal")
        if expected_goal is None or expected_goal.id != episode.goal_id:
            raise RuntimeError("attention episode's bound goal is no longer current")
        tools = self.initiative_tools()
        capabilities_available = bool(tools)
        instructions = self._attention_instructions(
            stimulus, episode, prior_memory, capabilities_available=capabilities_available,
            expected_goal=expected_goal,
        )
        action: str | None = None
        action_status: str | None = None
        action_result: str | None = None
        inspection_result: SelfInspectionResult | None = None
        perception_result: VisualPerceptionResult | None = None
        acquisitions: list[InitiativeAcquisitionOutcome] = []
        capability_requested = False

        async def execute_tool(call: CognitionToolCall) -> CognitionToolResult:
            nonlocal action, action_status, action_result
            nonlocal capability_requested, inspection_result, perception_result
            if capability_requested:
                return self._rejected_tool(
                    call.name, "initiative capability request already consumed",
                    log_prefix="INITIATIVE",
                )
            capability_requested = True
            LOGGER.info("[INITIATIVE] tool=%s status=requested", call.name)
            if call.name == INSPECT_SELF_TOOL.name:
                LOGGER.info(
                    "[ATTENTION] episode=E%s acquisition=1/%s tool=%s status=requested",
                    episode.id, MAX_AUTONOMOUS_ACQUISITIONS_PER_EPISODE, call.name,
                )
                result, inspection_result = self._execute_self_inspection(
                    call, expected_goal=expected_goal, autonomous=True
                )
            elif call.name == OBSERVE_SCENE_TOOL.name:
                LOGGER.info(
                    "[ATTENTION] episode=E%s acquisition=1/%s tool=%s status=requested",
                    episode.id, MAX_AUTONOMOUS_ACQUISITIONS_PER_EPISODE, call.name,
                )
                result, perception_result = await self._execute_visual_perception(
                    call, expected_goal=expected_goal, autonomous=True
                )
            else:
                action = call.name
                if self._active_goal is not expected_goal:
                    result = self._rejected_tool(
                        call.name, "attention episode's bound goal is no longer current",
                        log_prefix="INITIATIVE",
                    )
                else:
                    result = await self._execute_initiative_tool(call)
            try:
                result_status = json.loads(result.output).get("status", "rejected")
            except (json.JSONDecodeError, AttributeError):
                result_status = "rejected"
            if call.name in (INSPECT_SELF_TOOL.name, OBSERVE_SCENE_TOOL.name):
                acquisitions.append(InitiativeAcquisitionOutcome(
                    call.name, result_status, result.output,
                    inspection_result=inspection_result,
                    perception_result=perception_result,
                ))
                LOGGER.info(
                    "[ATTENTION] episode=E%s acquisition=1/%s tool=%s status=%s",
                    episode.id, MAX_AUTONOMOUS_ACQUISITIONS_PER_EPISODE,
                    call.name, result_status,
                )
            else:
                action_status = result_status
                action_result = result.output
            if action is not None:
                self.attention.record_action(action, action_status)
            return result

        LOGGER.info(
            "[INITIATIVE] backend=%s request=started capabilities=%s",
            backend.identifier, "enabled" if capabilities_available else "disabled",
        )
        try:
            response = await backend.respond(
                ACTION_INITIATIVE_REQUEST if capabilities_available else INITIATIVE_REQUEST,
                instructions=instructions,
                tools=tools,
                tool_executor=execute_tool if tools else None,
                refreshed_instructions=(
                    lambda: self._attention_instructions(
                        stimulus, episode, prior_memory, capabilities_available=True,
                        expected_goal=expected_goal,
                    )
                ) if tools else None,
            )
        except Exception:
            LOGGER.warning("[INITIATIVE] backend=%s request=failed", backend.identifier)
            raise
        LOGGER.info(
            "[INITIATIVE] backend=%s request=completed response_chars=%s",
            backend.identifier, len(response),
        )
        effects = []
        if action is not None:
            effects.append(InitiativeEffectOutcome(
                action, action_status or "rejected",
                action_result or '{"status": "rejected"}',
            ))
        continuation_completed = True
        if (acquisitions and expected_goal is not None
                and self.state is LifecycleState.RUNNING
                and self._active_goal is expected_goal):
            followup_completed, followup_effect = await self._request_acquisition_followup(
                stimulus, episode, expected_goal, prior_memory, acquisitions,
            )
            continuation_completed = followup_completed
            if followup_effect is not None:
                effects.append(followup_effect)
                action = followup_effect.name
                action_status = followup_effect.status
        if (
            self.options.initiative_continuation_enabled
            and continuation_completed
            and effects and effects[0].status == "applied"
            and expected_goal is not None
            and self.state is LifecycleState.RUNNING
            and self._active_goal is expected_goal
            and self.continuation_tools(effects[0].name)
        ):
            continuation_completed, continuation_effect = await self._request_continuation(
                stimulus, episode, expected_goal, prior_memory, effects[0],
                tuple(acquisitions),
            )
            if continuation_effect is not None:
                effects.append(continuation_effect)
        if (
            continuation_completed
            and self.options.initiative_goal_closure_enabled
            and effects
            and expected_goal is not None
            and self.state is LifecycleState.RUNNING
            and self._active_goal is expected_goal
        ):
            stimulus_outcome = GoalOutcomeStimulus(
                effects=tuple(effects),
                attention_kind=stimulus.kind,
                attention_source=stimulus.source,
                acquisitions=tuple(acquisitions),
            )
            await self._request_outcome_evaluation(
                stimulus_outcome, stimulus, episode, expected_goal, prior_memory
            )
        return InitiativeOutcome(response, action, action_status)

    def _acquisition_followup_instructions(
        self, followup: AcquisitionFollowupStimulus,
        stimulus: AttentionStimulus, episode: AttentionEpisode,
        expected_goal: ActiveGoal, working_memory,
    ) -> str:
        return "\n\n".join((
            compose_cognition_instructions(
                self.cognition_context(), self.temporal_context(), self.temporal_situation(),
                self.options.startup_prompt, working_memory,
                expected_goal if self._active_goal is expected_goal else None,
            ), episode.render(), stimulus.render(actions_enabled=None), followup.render(),
        ))

    async def _request_acquisition_followup(
        self, stimulus: AttentionStimulus, episode: AttentionEpisode,
        expected_goal: ActiveGoal, prior_memory,
        acquisitions: list[InitiativeAcquisitionOutcome],
    ) -> tuple[bool, InitiativeEffectOutcome | None]:
        backend = self._cognition_backend
        assert backend is not None
        # This helper is deliberately finite: one decision after acquisition #1,
        # followed by exactly one effect-only decision if #2 was attempted.
        followup = AcquisitionFollowupStimulus(tuple(acquisitions))
        tools = (*self.acquisition_tools(), *self.effect_tools())
        log_prefix = "ACQUISITION"
        action = status = result_text = None
        second_acquisition: InitiativeAcquisitionOutcome | None = None
        consumed = False

        async def execute_tool(call: CognitionToolCall) -> CognitionToolResult:
            nonlocal action, status, result_text, consumed, second_acquisition
            if consumed:
                return self._rejected_tool(
                    call.name, "acquisition follow-up already consumed",
                    log_prefix=log_prefix,
                )
            consumed = True
            available = (*self.acquisition_tools(), *self.effect_tools())
            inspection = perception = None
            if call.name in (INSPECT_SELF_TOOL.name, OBSERVE_SCENE_TOOL.name):
                LOGGER.info(
                    "[ATTENTION] episode=E%s acquisition=2/%s tool=%s status=requested",
                    episode.id, MAX_AUTONOMOUS_ACQUISITIONS_PER_EPISODE, call.name,
                )
            if (self.state is not LifecycleState.RUNNING
                    or self._active_goal is not expected_goal
                    or not any(tool.name == call.name for tool in available)):
                result = self._rejected_tool(
                    call.name, "capability is not available",
                    log_prefix=log_prefix,
                )
            elif call.name in (INSPECT_SELF_TOOL.name, OBSERVE_SCENE_TOOL.name):
                if call.name == INSPECT_SELF_TOOL.name:
                    result, inspection = self._execute_self_inspection(
                        call, expected_goal=expected_goal, autonomous=True
                    )
                else:
                    result, perception = await self._execute_visual_perception(
                        call, expected_goal=expected_goal, autonomous=True
                    )
            else:
                action = call.name
                result = await self._execute_initiative_tool(
                    call, available=available, log_prefix=log_prefix
                )
            result_text = result.output
            try:
                status = json.loads(result.output).get("status", "rejected")
            except (json.JSONDecodeError, AttributeError):
                status = "rejected"
            if call.name in (INSPECT_SELF_TOOL.name, OBSERVE_SCENE_TOOL.name):
                second_acquisition = InitiativeAcquisitionOutcome(
                    call.name, status, result.output,
                    inspection_result=inspection, perception_result=perception,
                )
                acquisitions.append(second_acquisition)
                LOGGER.info(
                    "[ATTENTION] episode=E%s acquisition=2/%s tool=%s status=%s",
                    episode.id, MAX_AUTONOMOUS_ACQUISITIONS_PER_EPISODE,
                    call.name, status,
                )
            if action is not None:
                self.attention.record_action(action, status)
            return result

        try:
            await backend.respond(
                ACQUISITION_FOLLOWUP_REQUEST,
                instructions=self._acquisition_followup_instructions(
                    followup, stimulus, episode, expected_goal, prior_memory
                ), tools=tools, tool_executor=execute_tool if tools else None,
                refreshed_instructions=lambda: self._acquisition_followup_instructions(
                    followup, stimulus, episode, expected_goal, prior_memory
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False, (None if action is None else InitiativeEffectOutcome(
                action, status or "rejected", result_text or '{"status": "rejected"}'
            ))
        if second_acquisition is not None and self._active_goal is expected_goal:
            final_completed, final_effect = await self._request_final_effect_decision(
                stimulus, episode, expected_goal, prior_memory, acquisitions
            )
            return final_completed, final_effect
        return True, (None if action is None else InitiativeEffectOutcome(
            action, status or "rejected", result_text or '{"status": "rejected"}'
        ))

    async def _request_final_effect_decision(
        self, stimulus: AttentionStimulus, episode: AttentionEpisode,
        expected_goal: ActiveGoal, prior_memory,
        acquisitions: list[InitiativeAcquisitionOutcome],
    ) -> tuple[bool, InitiativeEffectOutcome | None]:
        backend = self._cognition_backend
        assert backend is not None
        followup = AcquisitionFollowupStimulus(tuple(acquisitions))
        tools = self.effect_tools()
        action = status = result_text = None
        consumed = False

        async def execute_tool(call: CognitionToolCall) -> CognitionToolResult:
            nonlocal action, status, result_text, consumed
            if consumed:
                return self._rejected_tool(call.name, "final effect decision already consumed",
                                           log_prefix="ACQUISITION")
            consumed = True
            available = self.effect_tools()
            if (self.state is not LifecycleState.RUNNING
                    or self._active_goal is not expected_goal
                    or not any(tool.name == call.name for tool in available)):
                result = self._rejected_tool(call.name, "effect capability is not available",
                                             log_prefix="ACQUISITION")
            else:
                action = call.name
                result = await self._execute_initiative_tool(
                    call, available=available, log_prefix="ACQUISITION"
                )
            result_text = result.output
            try:
                status = json.loads(result.output).get("status", "rejected")
            except (json.JSONDecodeError, AttributeError):
                status = "rejected"
            if action is not None:
                self.attention.record_action(action, status)
            return result

        try:
            await backend.respond(
                ACQUISITION_FOLLOWUP_REQUEST,
                instructions=self._acquisition_followup_instructions(
                    followup, stimulus, episode, expected_goal, prior_memory
                ), tools=tools, tool_executor=execute_tool if tools else None,
                refreshed_instructions=(lambda: self._acquisition_followup_instructions(
                    followup, stimulus, episode, expected_goal, prior_memory
                )) if tools else None,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False, (None if action is None else InitiativeEffectOutcome(
                action, status or "rejected", result_text or '{"status": "rejected"}'
            ))
        return True, (None if action is None else InitiativeEffectOutcome(
            action, status or "rejected", result_text or '{"status": "rejected"}'
        ))

    def _continuation_instructions(
        self, continuation: InitiativeContinuationStimulus,
        stimulus: AttentionStimulus, episode: AttentionEpisode,
        expected_goal: ActiveGoal, working_memory,
    ) -> str:
        return "\n\n".join((
            compose_cognition_instructions(
                self.cognition_context(), self.temporal_context(), self.temporal_situation(),
                self.options.startup_prompt, working_memory,
                expected_goal if self._active_goal is expected_goal else None,
            ),
            episode.render(), stimulus.render(actions_enabled=None),
            continuation.render(),
        ))

    async def _request_continuation(
        self, stimulus: AttentionStimulus, episode: AttentionEpisode,
        expected_goal: ActiveGoal, prior_memory,
        first_effect: InitiativeEffectOutcome,
        acquisitions: tuple[InitiativeAcquisitionOutcome, ...] = (),
    ) -> tuple[bool, InitiativeEffectOutcome | None]:
        backend = self._cognition_backend
        assert backend is not None
        tools = self.continuation_tools(first_effect.name)
        if not tools:
            return True, None
        continuation = InitiativeContinuationStimulus(
            first_effect.name, first_effect.status, first_effect.runtime_result,
            stimulus.kind, stimulus.source,
            acquisitions,
        )
        action = status = result_text = None
        consumed = False

        async def execute_tool(call: CognitionToolCall) -> CognitionToolResult:
            nonlocal action, status, result_text, consumed
            if consumed:
                return self._rejected_tool(
                    call.name, "continuation capability request already consumed",
                    log_prefix="CONTINUATION",
                )
            consumed = True
            action = call.name
            self.attention.record_continuation(action=action)
            LOGGER.info("[CONTINUATION] tool=%s status=requested", call.name)
            available = self.continuation_tools(first_effect.name)
            if (
                first_effect.status != "applied"
                or self.state is not LifecycleState.RUNNING
                or self._active_goal is not expected_goal
                or call.name == first_effect.name
                or not any(tool.name == call.name for tool in available)
            ):
                result = self._rejected_tool(
                    call.name, "continuation capability is not available",
                    log_prefix="CONTINUATION",
                )
            else:
                result = await self._execute_initiative_tool(
                    call, available=available, log_prefix="CONTINUATION"
                )
            result_text = result.output
            try:
                status = json.loads(result.output).get("status", "rejected")
            except (json.JSONDecodeError, AttributeError):
                status = "rejected"
            self.attention.record_continuation(action_status=status)
            return result

        LOGGER.info(
            "[CONTINUATION] backend=%s request=started capabilities=enabled",
            backend.identifier,
        )
        try:
            response = await backend.respond(
                CONTINUATION_INITIATIVE_REQUEST,
                instructions=self._continuation_instructions(
                    continuation, stimulus, episode, expected_goal, prior_memory
                ),
                tools=tools,
                tool_executor=execute_tool,
                refreshed_instructions=lambda: self._continuation_instructions(
                    continuation, stimulus, episode, expected_goal, prior_memory
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.attention.record_continuation(state="failed")
            LOGGER.warning(
                "[CONTINUATION] backend=%s request=failed", backend.identifier
            )
            return False, (
                InitiativeEffectOutcome(action, status or "rejected", result_text or "")
                if action is not None else None
            )
        self.attention.record_continuation(state="completed", response=response)
        LOGGER.info(
            "[CONTINUATION] backend=%s request=completed response_chars=%s",
            backend.identifier, len(response),
        )
        return True, (
            InitiativeEffectOutcome(action, status or "rejected", result_text or "")
            if action is not None else None
        )

    def _outcome_instructions(
        self, outcome: GoalOutcomeStimulus, stimulus: AttentionStimulus,
        episode: AttentionEpisode, expected_goal: ActiveGoal, working_memory,
    ) -> str:
        return "\n\n".join((
            compose_cognition_instructions(
                self.cognition_context(), self.temporal_context(), self.temporal_situation(),
                self.options.startup_prompt, working_memory,
                expected_goal if self._active_goal is expected_goal else None,
            ),
            episode.render(), stimulus.render(actions_enabled=None),
            outcome.render(),
        ))

    async def _request_outcome_evaluation(
        self, outcome: GoalOutcomeStimulus, stimulus: AttentionStimulus,
        episode: AttentionEpisode, expected_goal: ActiveGoal, prior_memory,
    ) -> None:
        backend = self._cognition_backend
        assert backend is not None
        all_applied = bool(outcome.effects) and all(
            effect.status == "applied" for effect in outcome.effects
        )
        tools = self.outcome_tools(expected_goal, all_applied)
        LOGGER.info(
            "[OUTCOME] backend=%s request=started closure=%s", backend.identifier,
            "enabled" if tools else "disabled",
        )

        async def execute_tool(call: CognitionToolCall) -> CognitionToolResult:
            return self._execute_outcome_tool(
                call, expected_goal, all_applied
            )

        try:
            response = await backend.respond(
                OUTCOME_EVALUATION_REQUEST,
                instructions=self._outcome_instructions(
                    outcome, stimulus, episode, expected_goal, prior_memory
                ),
                tools=tools,
                tool_executor=execute_tool if tools else None,
                refreshed_instructions=(
                    lambda: self._outcome_instructions(
                        outcome, stimulus, episode, expected_goal, prior_memory
                    )
                ) if tools else None,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.attention.record_outcome(state="failed")
            LOGGER.warning("[OUTCOME] backend=%s request=failed", backend.identifier)
            return
        self.attention.record_outcome(state="completed", response=response)
        LOGGER.info(
            "[OUTCOME] backend=%s request=completed response_chars=%s",
            backend.identifier, len(response),
        )

    def outcome_tools(
        self, expected_goal: ActiveGoal, all_effects_applied: bool | str
    ) -> tuple[CognitionToolDefinition, ...]:
        """Project only same-goal successful completion for outcome evaluation."""
        if (
            self.options.initiative_goal_closure_enabled
            and (all_effects_applied is True or all_effects_applied == "applied")
            and self.state is LifecycleState.RUNNING
            and self._active_goal is expected_goal
        ):
            return (COMPLETE_GOAL_TOOL,)
        return ()

    def _execute_outcome_tool(
        self, call: CognitionToolCall, expected_goal: ActiveGoal,
        all_effects_applied: bool | str,
    ) -> CognitionToolResult:
        LOGGER.info("[OUTCOME] tool=%s status=requested", call.name)
        if call.name != COMPLETE_GOAL_TOOL.name:
            return self._rejected_tool(
                call.name, "tool is not available", log_prefix="OUTCOME"
            )
        try:
            self._tool_arguments(call, set())
            if COMPLETE_GOAL_TOOL not in self.outcome_tools(
                expected_goal, all_effects_applied
            ):
                raise RuntimeError("expected active goal is no longer current")
            self.resolve_goal("completed")
        except (json.JSONDecodeError, TypeError, ValueError, RuntimeError) as error:
            self.attention.record_outcome(closure="rejected")
            return self._rejected_tool(
                call.name, str(error), log_prefix="OUTCOME"
            )
        self.attention.record_outcome(closure="completed")
        LOGGER.info("[OUTCOME] tool=%s status=applied", call.name)
        return CognitionToolResult(json.dumps({"status": "completed"}, sort_keys=True))

    def initiative_tools(self) -> tuple[CognitionToolDefinition, ...]:
        """Project bounded autonomous capabilities at request time."""
        if not (self.options.initiative_enabled and
                self.state is LifecycleState.RUNNING and self._active_goal is not None):
            return ()
        return (*self.acquisition_tools(), *self.effect_tools())

    def acquisition_tools(self) -> tuple[CognitionToolDefinition, ...]:
        """Project only read-only autonomous acquisition capabilities."""
        if not (self.options.initiative_enabled and
                self.state is LifecycleState.RUNNING and self._active_goal is not None):
            return ()
        visual = (OBSERVE_SCENE_TOOL,) if self.visual_perception_available() else ()
        return (INSPECT_SELF_TOOL, *visual)

    def effect_tools(self) -> tuple[CognitionToolDefinition, ...]:
        """Project only currently permitted autonomous semantic effects."""
        if not (
            self.options.initiative_enabled
            and self.state is LifecycleState.RUNNING
            and self._active_goal is not None
        ):
            return ()
        body = self.body_backend
        tools = []
        if self.temporal.pending is None:
            tools.append(SCHEDULE_FOLLOWUP_TOOL)
        if (self.options.initiative_actions_enabled and body is not None and
                not body.is_physical and "orientation" in body.capabilities):
            tools.append(ORIENT_BODY_TOOL)
        if (self.options.initiative_messages_enabled and
                self._operator_message_sink is not None):
            tools.append(ADDRESS_OPERATOR_TOOL)
        return tuple(tools)

    def continuation_tools(
        self, first_effect_name: str
    ) -> tuple[CognitionToolDefinition, ...]:
        """Fresh initiative projection excluding the first semantic effect."""
        if not self.options.initiative_continuation_enabled:
            return ()
        return tuple(
            tool for tool in self.effect_tools() if tool.name != first_effect_name
        )

    def cognition_tools(self) -> tuple[CognitionToolDefinition, ...]:
        """Project currently safe cognition capabilities at request time."""
        body = self.body_backend
        tools = []
        if (
            body is not None
            and not body.is_physical
            and "orientation" in body.capabilities
        ):
            tools.append(ORIENT_BODY_TOOL)
        tools.append(SET_GOAL_TOOL if self._active_goal is None else RESOLVE_GOAL_TOOL)
        tools.append(INSPECT_SELF_TOOL)
        if self.visual_perception_available():
            tools.append(OBSERVE_SCENE_TOOL)
        return tuple(tools)

    def visual_perception_available(self) -> bool:
        camera = self.camera_backend
        return (
            self.state is LifecycleState.RUNNING
            and camera is not None and camera.is_running
            and self._visual_perception_backend is not None
        )

    async def _execute_cognition_tool(
        self, call: CognitionToolCall, *, expected_goal: ActiveGoal | None = None,
    ) -> CognitionToolResult:
        LOGGER.info("[COGNITION] tool=%s status=requested", call.name)
        if call.name == ORIENT_BODY_TOOL.name:
            return await self._execute_orient_body(
                call, available=self.cognition_tools(), source="cognition"
            )
        if call.name == SET_GOAL_TOOL.name:
            return self._execute_set_goal(call)
        if call.name == RESOLVE_GOAL_TOOL.name:
            return self._execute_resolve_goal(call, expected_goal=expected_goal)
        if call.name == INSPECT_SELF_TOOL.name:
            result, _ = self._execute_self_inspection(call)
            return result
        if call.name == OBSERVE_SCENE_TOOL.name:
            result, _ = await self._execute_visual_perception(call)
            return result
        return self._rejected_tool(call.name, "tool is not available")

    async def _execute_visual_perception(
        self, call: CognitionToolCall, *, expected_goal: ActiveGoal | None = None,
        autonomous: bool = False,
    ) -> tuple[CognitionToolResult, VisualPerceptionResult | None]:
        focus = ""
        try:
            arguments = self._tool_arguments(call, {"focus"})
            value = arguments["focus"]
            if type(value) is not str:
                raise ValueError("focus must be a string")
            if any(unicodedata.category(character) == "Cc" for character in value):
                raise ValueError("focus must not contain control characters")
            focus = value.strip()
            if not focus:
                raise ValueError("focus must be non-empty")
            if len(focus) > 300:
                raise ValueError("focus must be at most 300 characters")
            if not self.visual_perception_available():
                raise RuntimeError("visual perception is not available")
            if autonomous and (expected_goal is None or self._active_goal is not expected_goal):
                raise RuntimeError("expected active goal is no longer current")
            LOGGER.info("[PERCEPTION] modality=visual status=capture_requested")
            frame = self.camera_backend.capture_frame()  # type: ignore[union-attr]
            if len(frame.data) > MAX_CAMERA_FRAME_BYTES:
                raise ValueError(
                    f"camera frame exceeds {MAX_CAMERA_FRAME_BYTES} byte limit"
                )
            backend = self._visual_perception_backend
            assert backend is not None
            LOGGER.info(
                "[PERCEPTION] modality=visual backend=%s status=requested",
                backend.identifier,
            )
            result = await backend.interpret(frame, focus)
            if not isinstance(result, VisualPerceptionResult):
                raise TypeError("visual backend returned an invalid result")
            description = result.description.strip()
            if not description:
                raise ValueError("visual description must be non-empty")
            truncated = result.truncated or len(description) > 2000
            result = VisualPerceptionResult(focus, description[:2000], truncated)
        except Exception as error:
            LOGGER.info("[PERCEPTION] modality=visual status=rejected")
            if autonomous:
                self.attention.record_visual(
                    state="failed", focus=focus or None, status="rejected"
                )
            return CognitionToolResult(json.dumps({
                "status": "rejected", "error": str(error),
            }, sort_keys=True)), None
        LOGGER.info(
            "[PERCEPTION] modality=visual status=applied description_chars=%s",
            len(result.description),
        )
        if autonomous:
            self.attention.record_visual(state="completed", focus=focus, status="applied")
        return CognitionToolResult(json.dumps({
            "status": "applied", "focus": result.focus,
            "description": result.description, "truncated": result.truncated,
        }, sort_keys=True)), result

    def _execute_self_inspection(
        self, call: CognitionToolCall, *, expected_goal: ActiveGoal | None = None,
        autonomous: bool = False,
    ) -> tuple[CognitionToolResult, SelfInspectionResult | None]:
        area = "none"
        try:
            arguments = self._tool_arguments(call, {"area"})
            value = arguments["area"]
            if type(value) is not str or value not in SELF_INSPECTION_AREAS:
                raise ValueError("area must be network, storage, camera, or runtime")
            area = value
            LOGGER.info("[INSPECTION] area=%s status=requested", area)
            if self.state is not LifecycleState.RUNNING:
                raise RuntimeError("self-inspection requires a running application")
            if autonomous and (expected_goal is None or self._active_goal is not expected_goal):
                raise RuntimeError("expected active goal is no longer current")
            result = self._inspect_area(area)
        except Exception as error:
            LOGGER.info("[INSPECTION] area=%s status=rejected", area)
            if autonomous:
                self.attention.record_inspection(
                    state="failed", area=area if area != "none" else None,
                    status="rejected",
                )
            return CognitionToolResult(json.dumps({
                "status": "rejected", "error": str(error),
            }, sort_keys=True)), None
        LOGGER.info("[INSPECTION] area=%s status=applied", area)
        if autonomous:
            self.attention.record_inspection(state="completed", area=area, status="applied")
        return CognitionToolResult(json.dumps({
            "status": "applied", "area": result.area,
            "facts": [{"name": fact.name, "value": fact.value} for fact in result.facts],
        }, sort_keys=True)), result

    def _inspect_area(self, area: str) -> SelfInspectionResult:
        if area in ("network", "storage"):
            return self._self_inspector.inspect(area)
        if area == "camera":
            camera = self.camera_backend
            return SelfInspectionResult(area, (
                SelfInspectionFact("configured", str(camera is not None).lower()),
                SelfInspectionFact("backend", "none" if camera is None else camera.identifier),
                SelfInspectionFact("physical", "false" if camera is None else str(camera.is_physical).lower()),
                SelfInspectionFact("ready", "false" if camera is None else str(camera.is_running).lower()),
                SelfInspectionFact("capture_capable", "false" if camera is None else str(camera.is_running).lower()),
                SelfInspectionFact("visual_perception_enabled", str(self._visual_perception_backend is not None).lower()),
                SelfInspectionFact("visual_perception_backend", "none" if self._visual_perception_backend is None else self._visual_perception_backend.identifier),
            ))
        body = self.body_backend
        return SelfInspectionResult(area, (
            SelfInspectionFact("lifecycle", self.state.value),
            SelfInspectionFact("profile", self.profile.identifier),
            SelfInspectionFact("hardware_backend", self.hardware.identifier),
            SelfInspectionFact("hardware_physical", str(self.hardware.is_physical).lower()),
            SelfInspectionFact("body_backend", "none" if body is None else body.identifier),
            SelfInspectionFact("body_physical", "false" if body is None else str(body.is_physical).lower()),
            SelfInspectionFact("active_goal_present", str(self._active_goal is not None).lower()),
            SelfInspectionFact("working_memory_turns", str(len(self.working_memory.snapshot()))),
            SelfInspectionFact("working_memory_capacity", str(self.working_memory.capacity)),
            SelfInspectionFact("initiative_enabled", str(self.options.initiative_enabled).lower()),
            SelfInspectionFact("platform_attention_enabled", str(self.options.initiative_platform_attention_enabled).lower()),
            SelfInspectionFact("actions_enabled", str(self.options.initiative_actions_enabled).lower()),
            SelfInspectionFact("messages_enabled", str(self.options.initiative_messages_enabled).lower()),
            SelfInspectionFact("continuation_enabled", str(self.options.initiative_continuation_enabled).lower()),
            SelfInspectionFact("goal_closure_enabled", str(self.options.initiative_goal_closure_enabled).lower()),
            SelfInspectionFact("vision_enabled", str(self._visual_perception_backend is not None).lower()),
            SelfInspectionFact("temporal_followup_pending", str(self.temporal.pending is not None).lower()),
        ))

    async def _execute_initiative_tool(
        self, call: CognitionToolCall, *,
        available: tuple[CognitionToolDefinition, ...] | None = None,
        log_prefix: str = "INITIATIVE",
    ) -> CognitionToolResult:
        projected = self.initiative_tools() if available is None else available
        if call.name == ORIENT_BODY_TOOL.name:
            return await self._execute_orient_body(
                call, available=projected, source="initiative",
                log_prefix=log_prefix,
            )
        if call.name == ADDRESS_OPERATOR_TOOL.name:
            return await self._execute_address_operator(
                call, available=projected, log_prefix=log_prefix
            )
        if call.name == SCHEDULE_FOLLOWUP_TOOL.name:
            return self._execute_schedule_followup(
                call, available=projected, log_prefix=log_prefix
            )
        return self._rejected_tool(
            call.name, "tool is not available", log_prefix=log_prefix
        )

    def _execute_schedule_followup(
        self, call: CognitionToolCall, *,
        available: tuple[CognitionToolDefinition, ...] | None = None,
        log_prefix: str = "INITIATIVE",
    ) -> CognitionToolResult:
        projected = self.initiative_tools() if available is None else available
        try:
            if not any(tool.name == SCHEDULE_FOLLOWUP_TOOL.name for tool in projected):
                raise RuntimeError("tool is not available")
            arguments = self._tool_arguments(call, {"delay_seconds", "purpose"})
            delay = arguments["delay_seconds"]
            purpose_value = arguments["purpose"]
            if type(delay) is not int:
                raise ValueError("delay_seconds must be an integer")
            if not 10 <= delay <= 86400:
                raise ValueError("delay_seconds must be between 10 and 86400")
            if type(purpose_value) is not str:
                raise ValueError("purpose must be a string")
            purpose = purpose_value.strip()
            if not purpose:
                raise ValueError("purpose must be non-empty")
            if len(purpose) > 300:
                raise ValueError("purpose must be at most 300 characters")
            if any(unicodedata.category(char) == "Cc" for char in purpose):
                raise ValueError("purpose must not contain control characters")
            if self.state is not LifecycleState.RUNNING:
                raise RuntimeError("scheduling requires a running application")
            if not self.options.initiative_enabled:
                raise RuntimeError("initiative is disabled")
            goal = self._active_goal
            if goal is None:
                raise RuntimeError("no active goal exists")
            self.temporal.schedule(delay, purpose, goal)
        except (json.JSONDecodeError, TypeError, ValueError, RuntimeError) as error:
            return self._rejected_tool(call.name, str(error), log_prefix=log_prefix)
        LOGGER.info("[%s] tool=%s status=applied", log_prefix, call.name)
        return CognitionToolResult(json.dumps({
            "status": "applied", "delay_seconds": delay, "purpose": purpose,
        }, sort_keys=True))

    def temporal_followup_status(self) -> TemporalFollowupStatus:
        return self.temporal.status()

    def clear_temporal_followup(self) -> bool:
        return self.temporal.cancel("operator_clear")

    async def _execute_address_operator(
        self, call: CognitionToolCall, *,
        available: tuple[CognitionToolDefinition, ...] | None = None,
        log_prefix: str = "INITIATIVE",
    ) -> CognitionToolResult:
        projected = self.initiative_tools() if available is None else available
        if ADDRESS_OPERATOR_TOOL not in projected:
            return self._rejected_tool(
                call.name, "tool is not available", log_prefix=log_prefix
            )
        try:
            arguments = self._tool_arguments(call, {"message"})
            value = arguments["message"]
            if not isinstance(value, str):
                raise ValueError("message must be a string")
            message = value.strip()
            if not message:
                raise ValueError("message must be non-empty")
            if len(message) > MAX_OPERATOR_MESSAGE_CHARS:
                raise ValueError(
                    f"message must be at most {MAX_OPERATOR_MESSAGE_CHARS} characters"
                )
            if any(ord(character) < 32 or ord(character) == 127 for character in message):
                raise ValueError("message must not contain control characters")
            if self.state is not LifecycleState.RUNNING:
                raise RuntimeError("operator messaging requires a running application")
            if not self.options.initiative_messages_enabled:
                raise RuntimeError("initiative messages are disabled")
            sink = self._operator_message_sink
            if sink is None:
                raise RuntimeError("no operator message channel is configured")
            await sink.deliver(OperatorMessage(message, "initiative"))
        except Exception as error:
            LOGGER.info(
                "[INTERACTION] recipient=operator source=initiative status=rejected"
            )
            return self._rejected_tool(call.name, str(error), log_prefix=log_prefix)
        LOGGER.info(
            "[INTERACTION] recipient=operator source=initiative chars=%s status=delivered",
            len(message),
        )
        LOGGER.info("[%s] tool=%s status=applied", log_prefix, call.name)
        return CognitionToolResult(json.dumps({
            "status": "applied", "recipient": "operator", "message": message,
        }, sort_keys=True))

    async def _execute_orient_body(
        self, call: CognitionToolCall, *,
        available: tuple[CognitionToolDefinition, ...], source: str,
        log_prefix: str = "COGNITION",
    ) -> CognitionToolResult:
        if ORIENT_BODY_TOOL not in available:
            return self._rejected_tool(
                call.name, "tool is not available", log_prefix=log_prefix
            )
        try:
            arguments = json.loads(call.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
            if set(arguments) != {"yaw_degrees", "pitch_degrees"}:
                raise ValueError("exactly yaw_degrees and pitch_degrees are required")
            yaw = arguments["yaw_degrees"]
            pitch = arguments["pitch_degrees"]
            if any(type(value) not in (int, float) for value in (yaw, pitch)):
                raise ValueError("yaw_degrees and pitch_degrees must be numbers")
            if not all(math.isfinite(value) for value in (yaw, pitch)):
                raise ValueError("yaw_degrees and pitch_degrees must be finite")
            result = await self.set_body_orientation(
                yaw_degrees=yaw, pitch_degrees=pitch, source=source
            )
        except (json.JSONDecodeError, TypeError, ValueError, RuntimeError) as error:
            return self._rejected_tool(call.name, str(error), log_prefix=log_prefix)
        LOGGER.info(
            "[%s] tool=%s status=applied yaw_deg=%s pitch_deg=%s",
            log_prefix,
            call.name, result.yaw_degrees, result.pitch_degrees,
        )
        return CognitionToolResult(
            json.dumps(
                {
                    "status": "applied",
                    "yaw_degrees": result.yaw_degrees,
                    "pitch_degrees": result.pitch_degrees,
                },
                sort_keys=True,
            )
        )

    def _tool_arguments(self, call: CognitionToolCall, expected: set[str]) -> dict:
        arguments = json.loads(call.arguments)
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
        if set(arguments) != expected:
            raise ValueError(f"exactly {', '.join(sorted(expected))} is required")
        return arguments

    def _execute_set_goal(self, call: CognitionToolCall) -> CognitionToolResult:
        try:
            arguments = self._tool_arguments(call, {"description"})
            goal = self.set_goal(arguments["description"])
        except (json.JSONDecodeError, TypeError, ValueError, RuntimeError) as error:
            return self._rejected_tool(call.name, str(error))
        return CognitionToolResult(
            json.dumps(
                {"status": "active", "description": goal.description}, sort_keys=True
            )
        )

    def _execute_resolve_goal(
        self, call: CognitionToolCall, *, expected_goal: ActiveGoal | None = None,
    ) -> CognitionToolResult:
        try:
            if expected_goal is not None and self._active_goal is not expected_goal:
                raise RuntimeError("active goal changed since this decision was grounded")
            arguments = self._tool_arguments(call, {"outcome"})
            outcome = arguments["outcome"]
            goal = self.resolve_goal(outcome)
        except (json.JSONDecodeError, TypeError, ValueError, RuntimeError) as error:
            return self._rejected_tool(call.name, str(error))
        return CognitionToolResult(
            json.dumps(
                {"status": outcome, "description": goal.description}, sort_keys=True
            )
        )

    @staticmethod
    def _rejected_tool(
        name: str, error: str, *, log_prefix: str = "COGNITION"
    ) -> CognitionToolResult:
        LOGGER.info("[%s] tool=%s status=rejected", log_prefix, name)
        return CognitionToolResult(
            json.dumps({"status": "rejected", "error": error}, sort_keys=True)
        )

    def cognition_context(self) -> CognitionContext:
        """Copy an allow-listed projection of authoritative state for one request."""
        self.refresh_power_state()
        state = self._runtime_state
        platform = state.platform
        body = self.body_summary()
        camera = self.camera_summary()
        return CognitionContext(
            profile_id=self.profile.identifier,
            profile_name=self.profile.name,
            profile_description=self.profile.description,
            lifecycle=state.lifecycle.value,
            battery_voltage_v=state.power.battery_voltage_v,
            platform_hostname=None if platform is None else platform.hostname,
            platform_model=None if platform is None else platform.model,
            platform_system=None if platform is None else platform.system,
            platform_release=None if platform is None else platform.release,
            platform_machine=None if platform is None else platform.machine,
            platform_python_version=(
                None if platform is None else platform.python_version
            ),
            platform_uptime_seconds=None if platform is None else platform.uptime_seconds,
            platform_load_averages=None if platform is None else platform.load_averages,
            platform_memory_total_bytes=(
                None if platform is None else platform.memory_total_bytes
            ),
            platform_memory_available_bytes=(
                None if platform is None else platform.memory_available_bytes
            ),
            platform_cpu_temperature_celsius=(
                None if platform is None else platform.cpu_temperature_celsius
            ),
            hardware_backend=self.hardware.identifier,
            hardware_is_physical=self.hardware.is_physical,
            hardware_capabilities=tuple(self.hardware.capabilities),
            body_backend=None if body is None else body.backend,
            body_is_physical=None if body is None else body.is_physical,
            body_capabilities=None if body is None else body.capabilities,
            body_yaw_degrees=None if state.body is None else state.body.yaw_degrees,
            body_pitch_degrees=None if state.body is None else state.body.pitch_degrees,
            presence_status=(
                "unknown"
                if state.presence is None
                else "present"
                if state.presence.present
                else "absent"
            ),
            presence_source=None if state.presence is None else state.presence.source,
            camera_backend=None if camera is None else camera.backend,
            camera_is_physical=None if camera is None else camera.is_physical,
            camera_is_running=None if camera is None else camera.is_running,
        )

    async def set_body_orientation(
        self, *, yaw_degrees: float, pitch_degrees: float,
        source: str = "application",
    ) -> BodyState:
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Body orientation requires a running application")
        if self.body_backend is None:
            raise RuntimeError("No body backend is configured")
        if "orientation" not in self.body_backend.capabilities:
            raise RuntimeError("Body backend does not support orientation")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Body orientation source must be non-empty")
        previous = self._runtime_state.body
        result = await self.body_backend.set_orientation(yaw_degrees, pitch_degrees)
        self._runtime_state = replace(self._runtime_state, body=result)
        LOGGER.info(
            "[BODY] orientation yaw_deg=%s pitch_deg=%s",
            result.yaw_degrees,
            result.pitch_degrees,
        )
        if previous is not None and (
            previous.yaw_degrees != result.yaw_degrees
            or previous.pitch_degrees != result.pitch_degrees
        ):
            await self.events.publish(BodyOrientationChanged(
                source=source,
                previous_yaw_degrees=previous.yaw_degrees,
                previous_pitch_degrees=previous.pitch_degrees,
                yaw_degrees=result.yaw_degrees,
                pitch_degrees=result.pitch_degrees,
            ))
        return result

    async def observe_presence(self, *, present: bool, source: str) -> PresenceState:
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Presence observation requires a running application")
        if type(present) is not bool:
            raise TypeError("Presence value must be a bool")
        if not source or not source.strip():
            raise ValueError("Presence source must be non-empty")
        previous = self._runtime_state.presence
        current = PresenceState(present=present, source=source)
        self._runtime_state = replace(self._runtime_state, presence=current)
        if previous is None or previous.present != current.present:
            await self.events.publish(
                PresenceChanged(
                    source=source,
                    previous_present=None if previous is None else previous.present,
                    present=current.present,
                )
            )
        return current

    async def _stop_reflexes(self) -> None:
        failure: BaseException | None = None
        while self._started_reflexes:
            reflex = self._started_reflexes.pop()
            try:
                await reflex.stop()
            except BaseException as error:
                LOGGER.exception("[REFLEX] name=%s stop_failed", reflex.identifier)
                failure = failure or error
        if failure is not None:
            raise failure

    async def _stop_reflexes_for_cleanup(self) -> None:
        try:
            await self._stop_reflexes()
        except BaseException:
            # Startup's original failure remains authoritative.
            pass

    def body_summary(self) -> BodySummary | None:
        if self.body_backend is None:
            return None
        return BodySummary(
            backend=self.body_backend.identifier,
            is_physical=self.body_backend.is_physical,
            capabilities=tuple(self.body_backend.capabilities),
        )

    def camera_summary(self) -> CameraSummary | None:
        if self.camera_backend is None:
            return None
        return CameraSummary(
            backend=self.camera_backend.identifier,
            is_physical=self.camera_backend.is_physical,
            is_running=self.camera_backend.is_running,
        )

    def summary(self) -> RuntimeSummary:
        return RuntimeSummary(
            profile_id=self.profile.identifier,
            profile_name=self.profile.name,
            hardware_backend=self.hardware.identifier,
            hardware_is_physical=self.hardware.is_physical,
            capabilities=tuple(self.hardware.capabilities),
            startup_prompt_provided=self.options.startup_prompt is not None,
            lifecycle_status=self.state,
        )
