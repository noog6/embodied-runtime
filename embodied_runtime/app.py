"""Application lifecycle orchestration."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
import json
import logging
import math
import unicodedata
from time import monotonic, monotonic_ns
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
    CognitionError,
    CognitionContext,
    CognitionToolCall,
    CognitionToolDefinition,
    CognitionToolResult,
    GoalOutcomeStimulus,
    InitiativeAcquisitionOutcome,
    InitiativeEffectOutcome,
    TextCognitionBackend,
    WorkingMemory,
    WorkingMemoryObservation,
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
    MAX_OPERATOR_MESSAGE_CHARS, InteractionChannel, InteractionContext,
    InteractionInitiator, InteractionMode, OperatorMessage,
    OperatorDeliveryDestination, OperatorDeliveryRouteCatalog,
    OperatorMessageSink, VOICE_DIALOGUE, operator_delivery, render_dialogue_policy,
    render_notification_context, render_notification_policy,
    resolve_notification_route,
)
from embodied_runtime.jobs import (
    Job, JobContinuation, JobContinuationController, JobContinuationReadiness,
    JobContinuationState, JobReadinessEventType, JobWakeEvent,
    MAX_JOB_CONTINUATION_DELAY_SECONDS,
    MIN_JOB_CONTINUATION_DELAY_SECONDS,
    JobRun, JobRunStatus, JobStore, JobWorkDisposition, JobWorkOutcome,
    JOB_PROGRESS_BASES, JobProgress, JobProgressUpdate, validate_counter_name,
    ScheduledJobController, project_job_continuity_summary, render_job_continuity,
)
from embodied_runtime.jobs.model import MAX_RUN_SUMMARY_CHARS
from embodied_runtime.memory import (
    MAX_RECALL_QUERY_CHARS, MemoryAdmission, MemoryAdmissionProposal,
    MemoryRecallProjector, PersistentMemoryStore,
)
from embodied_runtime.observations import SemanticObservation, SemanticObservationFact
from embodied_runtime.observability import RunObservability
from embodied_runtime.inspection import (
    HostSelfInspector, SELF_INSPECTION_AREAS, SelfInspectionFact,
    SelfInspectionResult, SelfInspector,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.perception import (
    MAX_CAMERA_FRAME_BYTES, VisualPerceptionBackend, VisualPerceptionResult,
)
from embodied_runtime.reflexes import Reflex
from embodied_runtime.resources import (
    ResourceArbiter, ResourceBusyError, ResourceKey, ResourceLease, ResourceOwner,
)
from embodied_runtime.run_history import (
    MAX_GREP_QUERY_LENGTH, RunHistoryEvidenceReader, canonical_run_id,
)
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
from embodied_runtime.tasks import Task, TaskGoal, TaskStatus
from embodied_runtime.temporal import TemporalFollowupController, TemporalFollowupStatus
from embodied_runtime.temporal_context import TemporalContext, TemporalSituation
from embodied_runtime.voice import (
    MICROPHONE_RESOURCE,
    TextToSpeechProvider,
    VOICE_MICROPHONE_OWNER,
    VOICE_WAKE_MICROPHONE_OWNER,
    VoiceInteraction,
    VoiceProvider,
    VoiceSessionPolicy,
)

LOGGER = logging.getLogger(__name__)
OPERATOR_SOURCE: ContextVar[str] = ContextVar("operator_source", default="operator")
CAMERA_RESOURCE = ResourceKey("camera")
CAMERA_CAPTURE_OWNER = ResourceOwner("runtime", "camera_capture")
VISUAL_PERCEPTION_OWNER = ResourceOwner("runtime", "visual_perception")
SPEAKER_RESOURCE = ResourceKey("audio.speaker")
VOICE_SPEAKER_OWNER = ResourceOwner("runtime", "voice")


class SpeakerAuthorizedVoiceProvider:
    """Apply speaker authority to a voice provider's engagement cue only."""

    def __init__(self, provider: VoiceProvider, resources: ResourceArbiter) -> None:
        self._provider = provider
        self._resources = resources

    async def listen(self) -> str | None:
        return await self._provider.listen()

    async def stop_listening(self) -> None:
        await self._provider.stop_listening()

    async def play_engagement_cue(self) -> None:
        lease = self._resources.acquire(SPEAKER_RESOURCE, VOICE_SPEAKER_OWNER)
        try:
            await self._provider.play_engagement_cue()
        finally:
            self._resources.release(lease)

    async def close(self) -> None:
        await self._provider.close()


class SpeakerAuthorizedTextToSpeechProvider:
    """Apply speaker authority around application-composed TTS operations."""

    def __init__(
        self, provider: TextToSpeechProvider, resources: ResourceArbiter,
        observability: RunObservability | None = None,
    ) -> None:
        self._provider = provider
        self._resources = resources
        self._observability = observability

    async def speak(self, text: str) -> None:
        lease = self._resources.acquire(SPEAKER_RESOURCE, VOICE_SPEAKER_OWNER)
        try:
            await self._provider.speak(text)
            if self._observability is not None:
                provider = getattr(self._provider, "identifier", type(self._provider).__name__)
                self._observability.increment(
                    "tts_generations", dimension=("tts_providers", str(provider))
                )
                self._observability.increment("tts_characters", len(text))
                self._observability.event("voice", "tts_generation", "completed",
                    source=str(provider), metadata={"characters": len(text)})
        except asyncio.CancelledError:
            if self._observability is not None:
                self._observability.event("voice", "tts_generation", "cancelled")
            raise
        except BaseException as error:
            if self._observability is not None:
                self._observability.increment("voice_failures")
                self._observability.event("voice", "tts_generation", "failed",
                    severity="error", error=type(error).__name__)
            raise
        finally:
            self._resources.release(lease)

    async def close(self) -> None:
        # Physical providers defensively disable output here, so cleanup is also
        # a speaker mutation and must fail fast rather than affect another owner.
        lease = self._resources.acquire(SPEAKER_RESOURCE, VOICE_SPEAKER_OWNER)
        try:
            await self._provider.close()
        finally:
            self._resources.release(lease)

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


def deliver_message_tool(
    destinations: Sequence[OperatorDeliveryDestination],
) -> CognitionToolDefinition:
    """Build the operator-only effect from one captured authority set."""
    return CognitionToolDefinition(
        name="deliver_message",
        description=(
            "Use only when the operator explicitly requests or clearly authorizes "
            "delivery to one offered runtime-authorized semantic destination. Available "
            "destinations are permissions, not obligations. An applied result means the "
            "configured route accepted the delivery, not that the operator read, saw, or "
            "acknowledged it. Do not claim success until status=applied."
        ),
        parameters={
            "type": "object",
            "properties": {
                "destination": {
                    "type": "string",
                    "enum": [item.name for item in destinations],
                },
                "message": {"type": "string", "maxLength": MAX_OPERATOR_MESSAGE_CHARS},
            },
            "required": ["destination", "message"],
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

REPORT_JOB_OUTCOME_TOOL = CognitionToolDefinition(
    name="report_job_outcome",
    description=(
        "Report the outcome of this exact Job occurrence. Complete only when the "
        "evidence establishes its TaskGoal; fail only when authoritative evidence "
        "establishes that it cannot reasonably proceed; otherwise continue."
    ),
    parameters={
        "type": "object",
        "properties": {
            "disposition": {
                "type": "string", "enum": ["completed", "failed", "continue"],
            },
            "summary": {"type": "string", "minLength": 1,
                        "maxLength": MAX_RUN_SUMMARY_CHARS},
            "readiness": {"type": ["string", "null"], "enum": [
                "ready", "after_delay", "wait_for_operator", "wait_for_event", None,
            ]},
            "delay_seconds": {"type": ["integer", "null"],
                              "minimum": MIN_JOB_CONTINUATION_DELAY_SECONDS,
                              "maximum": MAX_JOB_CONTINUATION_DELAY_SECONDS},
            "event_type": {"type": ["string", "null"],
                           "enum": ["presence_changed", None]},
            "progress_update": {
                "anyOf": [
                    {"type": "null"},
                    {"type": "object", "properties": {
                        "counter": {
                            "type": "string",
                            "pattern": "^[a-z][a-z0-9_]{0,47}$",
                        },
                        "basis": {"type": "string", "enum": sorted(JOB_PROGRESS_BASES)},
                    }, "required": ["counter", "basis"],
                     "additionalProperties": False},
                ]
            },
        },
        "required": ["disposition", "summary", "readiness", "delay_seconds",
                     "event_type", "progress_update"],
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

RECALL_MEMORY_TOOL = CognitionToolDefinition(
    name="recall_memory",
    description=(
        "Recall durable long-term memories associated with one exact known entity name "
        "or alias when remembered knowledge may help the current concern. Persistent "
        "memory is available only through this deliberate lookup. Supply a concise name "
        "or alias, not the whole question. When an operator asks what you remember about "
        "yourself, use the exact current Robot name from runtime context; do not pass a "
        "pronoun as an alias. Results are historical stored knowledge, not "
        "current sensor evidence; no match and ambiguous matches are legitimate and "
        "must not be silently collapsed."
    ),
    parameters={
        "type": "object",
        "properties": {"query": {
            "type": "string", "minLength": 1, "maxLength": MAX_RECALL_QUERY_CHARS,
        }},
        "required": ["query"],
        "additionalProperties": False,
    },
)

INSPECT_RUN_HISTORY_TOOL = CognitionToolDefinition(
    name="inspect_run_history",
    description=(
        "Deliberately inspect bounded, content-filtered operational evidence from "
        "recorded runs. This read-only evidence is not remembered semantic knowledge."
    ),
    parameters={
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "Run selection: recent, current, previous, previous_day, or R<positive integer>.",
            },
            "query": {
                "type": ["string", "null"], "minLength": 1,
                "maxLength": MAX_GREP_QUERY_LENGTH,
                "description": "Null for metadata/overview; otherwise a literal log search.",
            },
        },
        "required": ["selector", "query"],
        "additionalProperties": False,
    },
)

REMEMBER_TOOL = CognitionToolDefinition(
    name="remember",
    description=(
        "Persist one durable fact, preference, or simple relationship that the operator "
        "directly stated in the current utterance and that is likely useful beyond this "
        "session. The subject must already exist in persistent memory by exact canonical "
        "name or alias. In an operator turn, you, your, yours, or yourself may be the "
        "subject reference for the current embodied runtime self; copy that exact reference "
        "and let the runtime resolve it from the current Robot name. Never rewrite it as "
        "the Robot name. Operator I, me, my, mine, and myself are not runtime-self "
        "references. Otherwise copy the exact subject name or alias used in the current "
        "operator utterance; never invent or create an entity. Select kind by meaning, not to "
        "bypass admission. Use a concise stable identifier for predicate. Copy value "
        "from the operator's wording rather than paraphrasing it. Copy evidence verbatim "
        "from the CURRENT operator utterance: use the shortest complete clause containing "
        "the exact subject reference and value (and related entity for a relationship). "
        "Never summarize, rewrite, infer, or combine assistant-generated language; the "
        "evidence becomes durable text. For fact/preference set both related fields to "
        "null. For relationship populate both: related_entity is the exact existing "
        "name/alias in evidence and equals value, while related_role is a simple identifier. "
        "Do not store guesses, assistant-generated conclusions, recalled information, "
        "transient state, jokes or sarcasm, uncertain implications, or ordinary "
        "conversation merely because this capability exists. This is a write effect; "
        "request at most one memory proposal."
    ),
    parameters={
        "type": "object",
        "properties": {
            "subject": {
                "type": "string", "minLength": 1, "maxLength": 256,
                "description": (
                    "Existing canonical name or exact alias copied from the current "
                    "operator utterance, or its exact bounded runtime-self reference "
                    "(you, your, yours, yourself); never create or invent an entity."
                ),
            },
            "kind": {
                "type": "string", "enum": ["fact", "preference", "relationship"],
                "description": (
                    "Semantic category of the statement; never select it to bypass "
                    "admission rules."
                ),
            },
            "predicate": {
                "type": "string", "minLength": 1, "maxLength": 64,
                "description": (
                    "Concise stable simple machine identifier, not prose; for example "
                    "preferred_editor, automatic_shutdown_voltage, or owner."
                ),
            },
            "value": {
                "type": "string", "minLength": 1, "maxLength": 500,
                "description": (
                    "Exact supported phrase from evidence, not a paraphrase; for "
                    "relationships exactly equal to related_entity."
                ),
            },
            "evidence": {
                "type": "string", "minLength": 1, "maxLength": 1000,
                "description": (
                    "Shortest complete clause copied verbatim from the CURRENT operator "
                    "utterance containing subject and value, plus related_entity for "
                    "relationships; never summarize, rewrite, infer, or use assistant "
                    "text. Becomes durable memory text."
                ),
            },
            "related_entity": {
                "type": ["string", "null"], "minLength": 1, "maxLength": 256,
                "description": (
                    "Null for fact/preference. For relationship, exact existing canonical "
                    "name or alias copied from evidence; must equal value and be paired "
                    "with related_role."
                ),
            },
            "related_role": {
                "type": ["string", "null"], "minLength": 1, "maxLength": 64,
                "description": (
                    "Null for fact/preference. For relationship, concise simple identifier "
                    "paired with related_entity; do not invent a relationship merely "
                    "because this field exists."
                ),
            },
        },
        "required": [
            "subject", "kind", "predicate", "value", "evidence",
            "related_entity", "related_role",
        ],
        "additionalProperties": False,
    },
)

SCHEDULE_FOLLOWUP_TOOL = CognitionToolDefinition(
    name="schedule_followup",
    description=(
        "Create the runtime's one session-local, one-shot relative follow-up. "
        "It is bound to the current active goal and creates fresh attention when due; "
        "it does not reserve future authority. Requesting this effect is not proof "
        "of scheduling: only a runtime result with status applied confirms the "
        "commitment; a rejected result must not be described as scheduled. This is "
        "a semantic effect."
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
JOB_OUTCOME_EVALUATION_REQUEST = (
    "Evaluate this exact Job occurrence once using only the authoritative Job, Task, "
    "acquisition, and effect evidence in the instructions. Request report_job_outcome "
    "with completed only when the TaskGoal is established, failed only when current "
    "authoritative evidence establishes the occurrence cannot reasonably proceed, and "
    "continue otherwise. For continue, report readiness: ready when useful work can "
    "proceed at the next ordinary opportunity; after_delay with a 1..86400 second "
    "delay when waiting itself is useful (choose the shortest reasonable delay); or "
    "wait_for_operator only when meaningful progress specifically requires explicit "
    "operator involvement; or wait_for_event with event_type presence_changed when a "
    "later authoritative presence transition would make reconsideration useful. "
    "Terminal outcomes require null readiness, delay, and event_type. Event waiting "
    "requires a null delay; every other readiness requires a null event_type. "
    "A wake event proves only its explicitly projected runtime fact; acquire fresh "
    "evidence for unrelated mutable conditions. The initiative response is "
    "model-generated commentary, not "
    "authoritative evidence, and any previous-work continuity context is deliberately "
    "excluded from this evidence bundle; neither can independently justify a terminal "
    "outcome. A continue outcome may propose at most one progress_update, naming a "
    "counter and one advertised current-episode evidence basis; the runtime alone "
    "increments it by one. Terminal outcomes require null progress_update. Insufficient "
    "information or one rejected optional capability "
    "normally means continue. Do not infer an outcome from prose and do not request work."
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
    jobs_auto_continue: bool = False
    jobs_heartbeat_seconds: float = 30.0
    jobs_max_auto_steps: int = 3
    jobs_scheduler_poll_seconds: float = 30.0


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


@dataclass(frozen=True, slots=True)
class _CurrentTaskBinding:
    """Session-local ownership of one running or paused Task activation."""

    task: Task
    active_goal: ActiveGoal | None


@dataclass(frozen=True, slots=True)
class CurrentJobRun:
    """Read-only view of one session-local JobRun-to-Task association."""

    job: Job
    run: JobRun
    task: Task


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
        operator_delivery_routes: OperatorDeliveryRouteCatalog | None = None,
        self_inspector: SelfInspector | None = None,
        visual_perception_backend: VisualPerceptionBackend | None = None,
        temporal_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        job_continuation_sleep: Callable[[float], Awaitable[None]] | None = None,
        job_scheduler_sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        voice_provider: VoiceProvider | None = None,
        text_to_speech_provider: TextToSpeechProvider | None = None,
        voice_policy: VoiceSessionPolicy = VoiceSessionPolicy(),
        voice_wake_words: list[str] | None = None,
        timezone_name: str = "UTC",
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        persistent_memory_store: PersistentMemoryStore | None = None,
        job_store: JobStore | None = None,
        run_history_evidence: RunHistoryEvidenceReader | None = None,
        resource_arbiter: ResourceArbiter | None = None,
        observability: RunObservability | None = None,
    ) -> None:
        self.profile = profile
        self.observability = observability or RunObservability()
        if cognition_backend is not None and hasattr(cognition_backend, "observability"):
            cognition_backend.observability = self.observability
        self.hardware = hardware
        self._timezone_name = timezone_name
        self._timezone = ZoneInfo(timezone_name)
        self._wall_clock = wall_clock
        self.options = options or ApplicationOptions()
        self.events = events or EventBus()
        self.resources = (
            resource_arbiter if resource_arbiter is not None else ResourceArbiter()
        )
        self.body_backend = body_backend
        self.camera_backend = camera_backend
        self._cognition_backend = cognition_backend
        self._active_operator_cognition_task: asyncio.Task[object] | None = None
        self._active_job_work_task: asyncio.Task[object] | None = None
        self._job_continuation: JobContinuation | None = None
        # One application-lifetime listener; readiness changes never add workers.
        self._job_event_subscription = (
            self.events.subscribe(PresenceChanged, self._on_job_presence_changed)
            if job_store is not None else None
        )
        self._operator_message_sink = operator_message_sink
        self._operator_delivery_routes = (
            operator_delivery_routes or OperatorDeliveryRouteCatalog()
        )
        self._self_inspector = self_inspector or HostSelfInspector()
        self._visual_perception_backend = visual_perception_backend
        self.working_memory = (
            working_memory if working_memory is not None else WorkingMemory()
        )
        self.persistent_memory = persistent_memory_store
        self.jobs = job_store
        self._run_history_evidence = run_history_evidence
        self._memory_recall = (
            MemoryRecallProjector(persistent_memory_store)
            if persistent_memory_store is not None else None
        )
        self._memory_admission = (
            MemoryAdmission(persistent_memory_store, runtime_self_name=profile.name)
            if persistent_memory_store is not None else None
        )
        self._persistent_memory_closed = False
        self._job_store_closed = False
        authorized_voice_provider = (
            SpeakerAuthorizedVoiceProvider(voice_provider, self.resources)
            if voice_provider is not None else None
        )
        authorized_tts_provider = (
            SpeakerAuthorizedTextToSpeechProvider(
                text_to_speech_provider, self.resources, self.observability
            )
            if text_to_speech_provider is not None else None
        )
        self.voice = VoiceInteraction(
            authorized_voice_provider,
            authorized_tts_provider,
            lambda text: self.handle_operator_utterance(
                text, interaction=VOICE_DIALOGUE
            ),
            voice_policy,
            wake_words=voice_wake_words,
            resources=self.resources,
        )
        self._active_goal: ActiveGoal | None = None
        self._current_task_binding: _CurrentTaskBinding | None = None
        self._current_job_run: CurrentJobRun | None = None
        self._job_progress: JobProgress | None = None
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
        self.episode_coordinator = AttentionEpisodeCoordinator(
            self._monotonic, self.observability
        )
        self._job_continuation_controller = (
            JobContinuationController(
                self.options.jobs_heartbeat_seconds,
                self._offer_job_continuation,
                sleep=job_continuation_sleep or temporal_sleep,
            )
            if self.jobs is not None and self.options.jobs_auto_continue else None
        )
        self._scheduled_job_controller = (
            ScheduledJobController(
                self.options.jobs_scheduler_poll_seconds,
                self._offer_scheduled_job,
                sleep=job_scheduler_sleep or asyncio.sleep,
            ) if self.jobs is not None else None
        )
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
    def timezone_name(self) -> str:
        return self._timezone_name

    @property
    def active_goal(self) -> ActiveGoal | None:
        return self._active_goal

    @property
    def current_task(self) -> Task | None:
        """Return the running or paused Task snapshot owned by this session."""
        binding = self._current_task_binding
        return None if binding is None else binding.task

    @property
    def current_job_run(self) -> CurrentJobRun | None:
        """Return the current volatile JobRun association, if one exists."""
        return self._current_job_run

    @property
    def job_continuation(self) -> JobContinuation | None:
        """Return the current session-local automatic continuation diagnostic."""
        return self._job_continuation

    @property
    def job_progress(self) -> JobProgress | None:
        """Return progress only when it belongs to the exact current occurrence."""
        progress = self._job_progress
        current = self._current_job_run
        if (progress is None or current is None
                or (progress.job_id, progress.run_id, progress.task_id)
                != (current.job.id, current.run.id, current.task.id)):
            return None
        return progress

    def _clear_stale_job_progress(self, binding: CurrentJobRun) -> None:
        """Clear only progress owned by a now-stale occurrence callback."""
        progress = self._job_progress
        occurrence = (binding.job.id, binding.run.id, binding.task.id)
        if progress is None or (progress.job_id, progress.run_id,
                                progress.task_id) != occurrence:
            return
        current = self._current_job_run
        if (current is not None
                and (current.job.id, current.run.id, current.task.id) == occurrence
                and current.run.status is JobRunStatus.RUNNING
                and current.task.status in (TaskStatus.RUNNING, TaskStatus.PAUSED)):
            # A pause (or pause/resume) invalidates the episode's ActiveGoal binding,
            # not the Job occurrence that owns already-committed progress.
            return
        self._job_progress = None

    def job_continuation_delay_remaining(self) -> int | None:
        """Return the rounded-up monotonic delay remaining for diagnostics."""
        continuation = self._job_continuation
        if (continuation is None
                or continuation.readiness is not JobContinuationReadiness.AFTER_DELAY
                or continuation.eligible_at_monotonic is None):
            return None
        return max(0, math.ceil(
            continuation.eligible_at_monotonic - self._monotonic()
        ))

    async def _on_job_presence_changed(self, event: PresenceChanged) -> None:
        """Record a matching wake; cognition remains owned by the normal gate."""
        continuation = self._job_continuation
        current = self._current_job_run
        task_binding = self._current_task_binding
        if (self.state is not LifecycleState.RUNNING
                or continuation is None
                or continuation.readiness is not JobContinuationReadiness.WAIT_FOR_EVENT
                or continuation.event_type is not JobReadinessEventType.PRESENCE_CHANGED
                or continuation.event_armed_after_ns is None
                or event.timestamp_ns <= continuation.event_armed_after_ns
                or continuation.event_satisfied
                or current is None
                or current.job.id != continuation.job_id
                or current.run.id != continuation.run_id
                or current.task.id != continuation.task_id
                or current.run.status is not JobRunStatus.RUNNING
                or task_binding is None
                or task_binding.task.id != continuation.task_id):
            return
        self._job_continuation = replace(
            continuation, event_satisfied=True,
            wake_event=JobWakeEvent(JobReadinessEventType.PRESENCE_CHANGED, event.present),
        )
        LOGGER.info(
            "[JOBS] job=JOB%s run=RUN%s continuation=event_satisfied event=%s",
            continuation.job_id, continuation.run_id, continuation.event_type.value,
        )

    @property
    def job_continuation_controller(self) -> JobContinuationController | None:
        return self._job_continuation_controller

    @property
    def scheduled_job_controller(self) -> ScheduledJobController | None:
        return self._scheduled_job_controller

    def start_job_run(self, job_id: int) -> CurrentJobRun:
        """Explicitly start one durable Job occurrence through Task coordination."""
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Starting a Job requires a running application")
        if self.jobs is None:
            raise RuntimeError("Jobs persistence is disabled")
        job = self.jobs.get_job(job_id)
        if job is None:
            raise RuntimeError(f"Job not found: JOB{job_id}")
        if not job.enabled:
            raise RuntimeError(f"Job is disabled: JOB{job.id}")
        if self._current_job_run is not None:
            raise RuntimeError("another JobRun is already current")
        if self._current_task_binding is not None:
            raise RuntimeError("an unrelated Task is already current")
        if self._active_goal is not None:
            raise RuntimeError("an unrelated active goal prevents Task start")

        run = self.jobs.create_run(job.id)
        return self._start_job_occurrence(job, run)

    def _start_job_occurrence(self, job: Job, run: JobRun) -> CurrentJobRun:
        """Bind one already-created occurrence through ordinary Task authority."""
        assert self.jobs is not None
        task = Task(
            f"Run JOB{job.id}: {job.name}",
            goal=TaskGoal(f"Complete JOB{job.id}: {job.name}"),
        )
        try:
            running_task = self.start_task(task)
        except BaseException:
            current = self.current_task
            if current is not None and current.id == task.id:
                try:
                    self.finish_task(TaskStatus.STOPPED)
                except BaseException:
                    LOGGER.exception(
                        "[JOBS] job=JOB%s run=RUN%s task=%s "
                        "compensation=task_cleanup_failed",
                        job.id, run.id, task.id,
                    )
            self._stop_unstarted_job_run(run, "Task execution did not start")
            raise
        try:
            running_run = self.jobs.transition_run(run.id, JobRunStatus.RUNNING)
        except BaseException:
            try:
                if self.current_task is not None and self.current_task.id == running_task.id:
                    self.finish_task(TaskStatus.STOPPED)
            except BaseException:
                LOGGER.exception(
                    "[JOBS] job=JOB%s run=RUN%s task=%s compensation=task_cleanup_failed",
                    job.id, run.id, running_task.id,
                )
            self._stop_unstarted_job_run(run, "Task execution did not start")
            raise
        binding = CurrentJobRun(job, running_run, running_task)
        self._current_job_run = binding
        self._job_progress = JobProgress(job.id, running_run.id, running_task.id)
        LOGGER.info(
            "[JOBS] job=JOB%s run=RUN%s task=%s status=started",
            job.id, running_run.id, running_task.id,
        )
        self.observability.increment("job_runs_started")
        self.observability.event("jobs", "job_run", "started",
            identifiers={"job_id": job.id, "job_run_id": running_run.id,
                         "task_id": running_task.id})
        return binding

    async def _offer_scheduled_job(self) -> None:
        """Start at most one currently-due daily occurrence and its first episode."""
        if self.state is not LifecycleState.RUNNING or self.jobs is None:
            return
        now = self._aware_wall_clock()
        due = []
        for schedule in self.jobs.list_schedules():
            local_now = now.astimezone(ZoneInfo(schedule.timezone))
            local_date = local_now.date().isoformat()
            hour, minute = map(int, schedule.local_time.split(":"))
            instant = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if local_now >= instant and schedule.last_started_local_date != local_date:
                due.append((instant.astimezone(UTC), schedule.job_id, schedule, local_date))
        due.sort(key=lambda item: (item[0], item[1]))
        global_reason = None
        if self.state is not LifecycleState.RUNNING:
            global_reason = "not_running"
        elif self._current_job_run is not None:
            global_reason = "current_job"
        elif self._current_task_binding is not None or self._active_goal is not None:
            global_reason = "task_busy"
        elif self._active_job_work_task is not None:
            global_reason = "work_active"
        elif self._cognition_backend is None:
            global_reason = "cognition_unavailable"
        elif self.episode_coordinator.operator_waiting:
            global_reason = "operator_waiting"
        elif self.episode_coordinator.current is not None:
            global_reason = "attention_busy"
        if global_reason is not None:
            if due:
                schedule, local_date = due[0][2:]
                LOGGER.info("[JOBS] job=JOB%s schedule=daily date=%s activation=deferred reason=%s",
                            schedule.job_id, local_date, global_reason)
            return
        selected = None
        for _, _, schedule, local_date in due:
            job = self.jobs.get_job(schedule.job_id)
            reason = ("schedule_disabled" if not schedule.enabled else
                      "job_missing" if job is None else
                      "job_disabled" if not job.enabled else None)
            if reason is not None:
                LOGGER.info("[JOBS] job=JOB%s schedule=daily date=%s activation=deferred reason=%s",
                            schedule.job_id, local_date, reason)
                continue
            selected = (schedule, local_date, job)
            break
        if selected is None:
            return
        schedule, local_date, job = selected
        # No await occurs between the final fairness checks, durable consumption,
        # Task creation, and attention claim.
        run = self.jobs.create_scheduled_run(schedule.job_id, local_date)
        if run is None:
            return
        binding = self._start_job_occurrence(job, run)
        prepared = self._validate_job_work_preconditions()
        episode = self._try_start_job_work_episode(binding, prepared[2])
        if episode is None:
            LOGGER.info("[JOBS] job=JOB%s run=RUN%s schedule=daily date=%s activation=deferred reason=attention_busy",
                        job.id, run.id, local_date)
            return
        LOGGER.info("[JOBS] job=JOB%s run=RUN%s schedule=daily date=%s activation=started",
                    job.id, run.id, local_date)
        task = asyncio.create_task(
            self._run_scheduled_initial_work(prepared, episode),
            name="job-scheduled-initial-work",
        )
        self._active_job_work_task = task

    async def _run_scheduled_initial_work(self, prepared, episode) -> None:
        try:
            outcome = await self._work_current_job_once(
                "scheduled", prepared=prepared, episode=episode,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.warning("[JOBS] scheduled initial work failed", exc_info=True)
            return
        if outcome.disposition is JobWorkDisposition.CONTINUE:
            self._arm_job_continuation(outcome, source="scheduled")

    def _stop_unstarted_job_run(self, run: JobRun, summary: str) -> None:
        """Best-effort compensation for a pending occurrence that did not start."""
        assert self.jobs is not None
        try:
            self.jobs.transition_run(
                run.id, JobRunStatus.STOPPED, outcome_summary=summary
            )
        except BaseException:
            LOGGER.exception("[JOBS] run=RUN%s compensation=stop_failed", run.id)

    def finish_job_run(
        self, status: JobRunStatus, summary: str | None = None,
    ) -> CurrentJobRun:
        """Terminalize the bound Task, then persist the matching JobRun result."""
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Finishing a Job requires a running application")
        if not isinstance(status, JobRunStatus):
            raise TypeError("status must be a JobRunStatus")
        if status not in (
            JobRunStatus.COMPLETED, JobRunStatus.FAILED, JobRunStatus.STOPPED
        ):
            raise ValueError("status must be completed, failed, or stopped")
        if summary is not None:
            if not isinstance(summary, str):
                raise TypeError("summary must be a string or None")
            summary = summary.strip()
            if not summary:
                raise ValueError("summary must not be empty")
            if len(summary) > MAX_RUN_SUMMARY_CHARS:
                raise ValueError(
                    f"summary must be at most {MAX_RUN_SUMMARY_CHARS} characters"
                )
        binding = self._current_job_run
        if binding is None:
            raise RuntimeError("no current JobRun")
        self._clear_job_continuation(status.value)
        task_status = TaskStatus(status.value)
        task = binding.task
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED):
            if task.status is not task_status:
                raise RuntimeError(
                    f"JobRun terminal retry must remain {task.status.value}"
                )
            if self.current_task is not None:
                raise RuntimeError("current JobRun Task binding is inconsistent")
        else:
            current = self.current_task
            if current is None or current.id != task.id:
                raise RuntimeError("current JobRun Task binding is inconsistent")
            task = self.finish_task(task_status)
            binding = CurrentJobRun(binding.job, binding.run, task)
            self._current_job_run = binding

        assert self.jobs is not None
        kwargs = (
            {"error_summary": summary}
            if status is JobRunStatus.FAILED
            else {"outcome_summary": summary}
        )
        run = self.jobs.transition_run(binding.run.id, status, **kwargs)
        finished = CurrentJobRun(binding.job, run, task)
        self._current_job_run = None
        self._job_progress = None
        LOGGER.info(
            "[JOBS] job=JOB%s run=RUN%s task=%s status=%s",
            binding.job.id, run.id, task.id, status.value,
        )
        counter = {JobRunStatus.COMPLETED: "job_runs_completed",
                   JobRunStatus.FAILED: "job_runs_failed",
                   JobRunStatus.STOPPED: "job_runs_stopped"}[status]
        self.observability.increment(counter)
        self.observability.event("jobs", "job_run", status.value,
            identifiers={"job_id": binding.job.id, "job_run_id": run.id,
                         "task_id": task.id})
        return finished

    async def work_current_job_once(self) -> JobWorkOutcome:
        """Perform one explicitly requested, finite episode for the current JobRun."""
        # An explicit request supersedes any old automatic grant. Its result alone
        # decides whether a fresh grant is created.
        if self._active_job_work_task is not None:
            raise RuntimeError("another Job work episode is already active")
        prepared = self._validate_job_work_preconditions()
        previous_work_summary = self._valid_job_continuity_summary(*prepared[:2])
        self._job_continuation = None
        try:
            outcome = await self._work_current_job_once(
                "manual", prepared=prepared,
                previous_work_summary=previous_work_summary,
            )
        except asyncio.CancelledError:
            self._set_job_continuation_awaiting_operator()
            raise
        if outcome.disposition is JobWorkDisposition.CONTINUE:
            self._arm_job_continuation(outcome)
        return outcome

    async def _work_current_job_once(
        self,
        invocation: str,
        *,
        prepared: tuple[CurrentJobRun, _CurrentTaskBinding, ActiveGoal] | None = None,
        episode: AttentionEpisode | None = None,
        previous_work_summary: str | None = None,
        wake_event: JobWakeEvent | None = None,
    ) -> JobWorkOutcome:
        """Shared one-shot executor for manual and heartbeat work."""
        binding, task_binding, goal = (
            self._validate_job_work_preconditions() if prepared is None else prepared
        )
        if episode is None:
            episode = self._try_start_job_work_episode(binding, goal)
            if episode is None:
                raise RuntimeError(
                    "another attention episode is already active or an operator is waiting"
                )
        current_async_task = asyncio.current_task()
        if current_async_task is None:
            self.episode_coordinator.close(episode, "error")
            raise RuntimeError("Job work requires an asyncio task")
        if self._active_job_work_task not in (None, current_async_task):
            self.episode_coordinator.close(episode, "error")
            raise RuntimeError("another Job work episode is already active")
        self._active_job_work_task = current_async_task
        target = "unassigned" if binding.job.target is None else str(binding.job.target)
        wake_facts = () if wake_event is None else (
            SemanticObservationFact("continuation_wake_event", wake_event.event_type.value),
            SemanticObservationFact("presence_reported", str(wake_event.present).lower()),
            SemanticObservationFact(
                "wake_event_authority",
                "The runtime event occurred after this JobRun began waiting. Its "
                "presence_reported field is authoritative only for reported presence; "
                "it establishes no identity, object visibility, safety, or other "
                "mutable condition.",
            ),
        )
        stimulus = AttentionStimulus(SemanticObservation(
            "job_run_work", f"JOB{binding.job.id}/RUN{binding.run.id}", (
                SemanticObservationFact("job_id", str(binding.job.id)),
                SemanticObservationFact("run_id", str(binding.run.id)),
                SemanticObservationFact("job_name", binding.job.name),
                SemanticObservationFact("job_description", binding.job.description),
                SemanticObservationFact("job_target", target),
                SemanticObservationFact("task_id", str(binding.task.id)),
                SemanticObservationFact("task_description", binding.task.description),
                SemanticObservationFact("task_goal", binding.task.goal.description),
                SemanticObservationFact(
                    "authority",
                    "This is one runtime-owned bounded work episode for an existing "
                    "JobRun. The Job definition is durable responsibility context, not "
                    "a new operator utterance.",
                ),
                *wake_facts,
            )
        ))
        LOGGER.info("[JOBS] job=JOB%s run=RUN%s episode=E%s work=started source=%s",
                    binding.job.id, binding.run.id, episode.id, invocation)
        continuity = project_job_continuity_summary(previous_work_summary)
        if continuity is not None:
            LOGGER.info(
                "[JOBS] job=JOB%s run=RUN%s episode=E%s continuity=provided chars=%s",
                binding.job.id, binding.run.id, episode.id, len(continuity),
            )
        reason: EpisodeCompletionReason = "error"
        try:
            initiative = await self._request_initiative(
                stimulus, episode, evaluate_goal_outcome=False, job_work=True,
                previous_work_summary=continuity,
            )
            disposition, summary, readiness, delay_seconds, event_type, progress_update = await self._request_job_outcome(
                binding, task_binding, goal, episode, stimulus, initiative,
                wake_event=wake_event,
            )
            if (
                disposition is JobWorkDisposition.CONTINUE
                and not self._job_work_binding_matches(binding, task_binding, goal)
            ):
                reason = "stale_goal"
            else:
                reason = "handled" if initiative.action is not None else "no_action"
            LOGGER.info(
                "[JOBS] job=JOB%s run=RUN%s episode=E%s work=completed disposition=%s",
                binding.job.id, binding.run.id, episode.id, disposition.value,
            )
            self.observability.increment(
                "manual_job_work" if invocation == "manual" else "automatic_job_work",
                dimension=("job_work_outcomes", disposition.value),
            )
            if invocation not in ("manual", "scheduled"):
                self.observability.increment("continuations_accepted")
            self.observability.event("jobs", "work_episode", "completed",
                source=invocation,
                identifiers={"job_id": binding.job.id,
                             "job_run_id": binding.run.id,
                             "task_id": binding.task.id,
                             "episode_id": episode.id},
                metadata={"outcome": disposition.value})
            return JobWorkOutcome(
                binding.job.id, binding.run.id, binding.task.id, episode.id,
                disposition, summary, initiative.response,
                initiative.action, initiative.action_status, readiness, delay_seconds,
                event_type, progress_update,
            )
        except asyncio.CancelledError:
            reason = "cancelled"
            LOGGER.info("[JOBS] job=JOB%s run=RUN%s episode=E%s work=cancelled",
                        binding.job.id, binding.run.id, episode.id)
            raise
        except Exception:
            LOGGER.warning("[JOBS] job=JOB%s run=RUN%s episode=E%s work=failed",
                           binding.job.id, binding.run.id, episode.id)
            raise
        finally:
            try:
                await self._finish_job_work_episode(episode, reason)
            finally:
                if self._active_job_work_task is current_async_task:
                    self._active_job_work_task = None

    def _arm_job_continuation(self, outcome: JobWorkOutcome, *, source: str = "manual") -> None:
        if not self.options.jobs_auto_continue or self.jobs is None:
            self._job_continuation = None
            return
        current = self._current_job_run
        if current is None or current.run.id != outcome.run_id:
            return
        if outcome.readiness is None:
            self._job_continuation = None
            return
        eligible_at = (
            self._monotonic() + outcome.delay_seconds
            if outcome.readiness is JobContinuationReadiness.AFTER_DELAY
            and outcome.delay_seconds is not None else None
        )
        event_type = outcome.event_type
        event_armed_after_ns = (
            monotonic_ns()
            if outcome.readiness is JobContinuationReadiness.WAIT_FOR_EVENT else None
        )
        self._job_continuation = JobContinuation(
            outcome.job_id, outcome.run_id, outcome.task_id,
            JobContinuationState.ARMED, self.options.jobs_max_auto_steps,
            outcome.summary, outcome.readiness, eligible_at, event_type,
            event_armed_after_ns,
        )
        LOGGER.info(
            "[JOBS] job=JOB%s run=RUN%s continuation=armed readiness=%s%s remaining=%s source=%s",
            outcome.job_id, outcome.run_id, outcome.readiness.value,
            (f" delay_s={outcome.delay_seconds}" if outcome.delay_seconds is not None
             else f" event={event_type.value}" if event_type is not None else ""),
            self.options.jobs_max_auto_steps, source,
        )

    def _set_job_continuation_awaiting_operator(self) -> None:
        continuation = self._job_continuation
        if continuation is not None:
            self._job_continuation = replace(
                continuation, state=JobContinuationState.AWAITING_OPERATOR,
            )

    def _valid_job_continuity_summary(
        self, binding: CurrentJobRun, task_binding: _CurrentTaskBinding,
    ) -> str | None:
        """Return continuity only for the exact current occurrence association."""
        continuation = self._job_continuation
        if (continuation is None
                or continuation.job_id != binding.job.id
                or continuation.run_id != binding.run.id
                or continuation.task_id != binding.task.id
                or task_binding.task.id != binding.task.id):
            return None
        return continuation.last_summary

    def _clear_job_continuation(self, reason: str) -> None:
        continuation = self._job_continuation
        if continuation is None:
            return
        LOGGER.info(
            "[JOBS] job=JOB%s run=RUN%s continuation=cleared reason=%s",
            continuation.job_id, continuation.run_id, reason,
        )
        self._job_continuation = None

    def _try_start_job_work_episode(
        self, binding: CurrentJobRun, goal: ActiveGoal,
    ) -> AttentionEpisode | None:
        """Atomically claim the attention grant for one bounded Job episode."""
        return self.episode_coordinator.try_start(
            "job_run", f"JOB{binding.job.id}/RUN{binding.run.id}",
            f"Perform one bounded work episode for JOB{binding.job.id}/RUN{binding.run.id}",
            goal.id,
        )

    def _offer_job_continuation(self) -> None:
        """Validate and schedule at most one separately owned automatic episode."""
        continuation = self._job_continuation
        if continuation is None or continuation.state is not JobContinuationState.ARMED:
            return
        if self.state is not LifecycleState.RUNNING or self.jobs is None \
                or not self.options.jobs_auto_continue:
            return
        current = self._current_job_run
        task_binding = self._current_task_binding
        if (current is None or current.job.id != continuation.job_id
                or current.run.id != continuation.run_id
                or current.run.status is not JobRunStatus.RUNNING
                or current.task.id != continuation.task_id
                or task_binding is None
                or task_binding.task.id != continuation.task_id):
            self._clear_job_continuation("binding_changed")
            return
        if continuation.readiness is JobContinuationReadiness.WAIT_FOR_OPERATOR:
            return
        if continuation.readiness is JobContinuationReadiness.WAIT_FOR_EVENT:
            if (continuation.event_type is None
                    or continuation.event_armed_after_ns is None):
                self._clear_job_continuation("invalid_readiness_state")
                return
            if not continuation.event_satisfied or continuation.wake_event is None:
                return
        if continuation.readiness is JobContinuationReadiness.AFTER_DELAY:
            if continuation.eligible_at_monotonic is None:
                self._clear_job_continuation("invalid_readiness_state")
                return
            if self._monotonic() < continuation.eligible_at_monotonic:
                return
        if current.task.status is TaskStatus.PAUSED:
            self._log_job_continuation_deferred(continuation, "task_paused")
            return
        if (current.task.status is not TaskStatus.RUNNING
                or task_binding.active_goal is None
                or self._active_goal is not task_binding.active_goal):
            self._clear_job_continuation("binding_changed")
            return
        if self._active_job_work_task is not None:
            self._log_job_continuation_deferred(continuation, "work_active")
            return
        if self.episode_coordinator.operator_waiting:
            self._log_job_continuation_deferred(continuation, "operator_waiting")
            return
        if self.episode_coordinator.current is not None:
            self._log_job_continuation_deferred(continuation, "attention_busy")
            return
        if continuation.automatic_steps_remaining <= 0:
            self._job_continuation = replace(
                continuation, state=JobContinuationState.AWAITING_OPERATOR,
            )
            return
        # The attention claim is the acceptance boundary. Nothing is charged until
        # this synchronous single-flight operation succeeds; a contender that wins
        # after the preliminary checks therefore remains an ordinary deferral.
        episode = self._try_start_job_work_episode(current, task_binding.active_goal)
        if episode is None:
            reason = (
                "operator_waiting"
                if self.episode_coordinator.operator_waiting
                else "attention_busy"
            )
            self._log_job_continuation_deferred(continuation, reason)
            return
        remaining = continuation.automatic_steps_remaining - 1
        self._job_continuation = replace(
            continuation, automatic_steps_remaining=remaining,
            event_satisfied=False, wake_event=None,
        )
        task = asyncio.create_task(
            self._run_automatic_job_work(
                (current, task_binding, task_binding.active_goal), episode,
                continuation.last_summary, continuation.wake_event,
            ),
            name="job-continuation-work",
        )
        self._active_job_work_task = task
        LOGGER.info(
            "[JOBS] job=JOB%s run=RUN%s continuation=accepted remaining=%s source=heartbeat",
            continuation.job_id, continuation.run_id, remaining,
        )

    def _log_job_continuation_deferred(
        self, continuation: JobContinuation, reason: str,
    ) -> None:
        LOGGER.info(
            "[JOBS] job=JOB%s run=RUN%s continuation=deferred reason=%s",
            continuation.job_id, continuation.run_id, reason,
        )

    async def _run_automatic_job_work(
        self,
        prepared: tuple[CurrentJobRun, _CurrentTaskBinding, ActiveGoal],
        episode: AttentionEpisode,
        previous_work_summary: str | None,
        wake_event: JobWakeEvent | None,
    ) -> None:
        continuation = self._job_continuation
        binding, task_binding, goal = prepared
        if (continuation is None
                or continuation.job_id != binding.job.id
                or continuation.run_id != binding.run.id
                or continuation.task_id != binding.task.id
                or not self._job_work_binding_matches(binding, task_binding, goal)):
            self.episode_coordinator.close(episode, "stale_goal")
            if self._active_job_work_task is asyncio.current_task():
                self._active_job_work_task = None
            return
        try:
            outcome = await self._work_current_job_once(
                "heartbeat", prepared=prepared, episode=episode,
                previous_work_summary=previous_work_summary, wake_event=wake_event,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            current = self._job_continuation
            if current is not None:
                self._job_continuation = replace(
                    current, state=JobContinuationState.AWAITING_OPERATOR,
                )
                LOGGER.warning(
                    "[JOBS] job=JOB%s run=RUN%s continuation=awaiting_operator reason=work_error",
                    current.job_id, current.run_id,
                )
            return
        if outcome.disposition is not JobWorkDisposition.CONTINUE:
            return
        current = self._job_continuation
        if current is None:
            return
        if outcome.readiness is None:
            self._job_continuation = replace(
                current, state=JobContinuationState.AWAITING_OPERATOR,
            )
            LOGGER.warning(
                "[JOBS] job=JOB%s run=RUN%s continuation=awaiting_operator reason=invalid_outcome",
                current.job_id, current.run_id,
            )
            return
        eligible_at = (
            self._monotonic() + outcome.delay_seconds
            if outcome.readiness is JobContinuationReadiness.AFTER_DELAY
            and outcome.delay_seconds is not None else None
        )
        updated = replace(
            current, last_summary=outcome.summary, readiness=outcome.readiness,
            eligible_at_monotonic=eligible_at, event_type=outcome.event_type,
            event_armed_after_ns=(
                monotonic_ns()
                if outcome.readiness is JobContinuationReadiness.WAIT_FOR_EVENT
                else None
            ),
            event_satisfied=False, wake_event=None,
        )
        if current.automatic_steps_remaining == 0:
            self._job_continuation = replace(
                updated, state=JobContinuationState.AWAITING_OPERATOR,
            )
            LOGGER.info(
                "[JOBS] job=JOB%s run=RUN%s continuation=awaiting_operator reason=budget_exhausted",
                current.job_id, current.run_id,
            )
        else:
            self._job_continuation = updated
            if outcome.readiness is JobContinuationReadiness.WAIT_FOR_OPERATOR:
                LOGGER.info(
                    "[JOBS] job=JOB%s run=RUN%s continuation=awaiting_operator reason=model_readiness",
                    current.job_id, current.run_id,
                )

    async def _finish_job_work_episode(
        self, episode: AttentionEpisode, reason: EpisodeCompletionReason,
    ) -> None:
        """Close Job attention, then offer independently due temporal work."""
        self.episode_coordinator.close(episode, reason)
        if self.state is LifecycleState.RUNNING:
            await self.attention.release_temporal_due()

    def _validate_job_work_preconditions(
        self,
    ) -> tuple[CurrentJobRun, _CurrentTaskBinding, ActiveGoal]:
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Job work requires a running application")
        if self._cognition_backend is None:
            raise RuntimeError("No cognition backend is configured")
        if self.jobs is None:
            raise RuntimeError("Jobs persistence is disabled")
        binding = self._current_job_run
        if binding is None:
            raise RuntimeError("no current JobRun")
        if binding.run.status is not JobRunStatus.RUNNING:
            raise RuntimeError("current JobRun must be running")
        task_binding = self._current_task_binding
        if task_binding is None or task_binding.task is not binding.task:
            raise RuntimeError("current JobRun Task binding is inconsistent")
        if binding.task.status is TaskStatus.PAUSED:
            raise RuntimeError("current JobRun Task is paused; resume it explicitly")
        if binding.task.status is not TaskStatus.RUNNING:
            raise RuntimeError("current JobRun Task must be running")
        goal = task_binding.active_goal
        if goal is None or self._active_goal is not goal:
            raise RuntimeError("current JobRun Task ActiveGoal binding is inconsistent")
        self._validate_current_task_binding(task_binding)
        return binding, task_binding, goal

    def _job_work_binding_matches(
        self, binding: CurrentJobRun, task_binding: _CurrentTaskBinding,
        goal: ActiveGoal,
    ) -> bool:
        current = self._current_job_run
        return (
            self.state is LifecycleState.RUNNING
            and current is binding
            and current.job.id == binding.job.id
            and current.run.id == binding.run.id
            and current.run.status is JobRunStatus.RUNNING
            and self._current_task_binding is task_binding
            and self.current_task is binding.task
            and binding.task.status is TaskStatus.RUNNING
            and task_binding.active_goal is goal
            and self._active_goal is goal
        )

    async def _request_job_outcome(
        self, binding: CurrentJobRun, task_binding: _CurrentTaskBinding,
        goal: ActiveGoal, episode: AttentionEpisode, stimulus: AttentionStimulus,
        initiative: InitiativeOutcome,
        *, wake_event: JobWakeEvent | None = None,
    ) -> tuple[JobWorkDisposition, str | None, JobContinuationReadiness | None,
               int | None, JobReadinessEventType | None, JobProgressUpdate | None]:
        backend = self._cognition_backend
        assert backend is not None
        proposed_disposition: JobWorkDisposition | None = None
        proposed_summary: str | None = None
        proposed_readiness: JobContinuationReadiness | None = None
        proposed_delay_seconds: int | None = None
        proposed_event_type: JobReadinessEventType | None = None
        proposed_progress_update: JobProgressUpdate | None = None
        consumed = False

        def evidence_lines() -> list[str]:
            lines = [
                "Job outcome evaluation", stimulus.render(actions_enabled=None),
                "Non-authoritative model commentary from this current episode "
                "(not evidence):",
                f"  initiative_response: {initiative.response}",
                "Ordered runtime-produced acquisition evidence:",
            ]
            lines.extend(
                f"  {index}. {item.capability} status={item.status} result={item.runtime_result}"
                for index, item in enumerate(initiative.acquisitions, 1)
            )
            progress = self.job_progress
            lines.extend((progress.render() if progress is not None else
                          JobProgress(binding.job.id, binding.run.id,
                                      binding.task.id).render()).splitlines())
            lines.append("Available progress bases this episode:")
            bases = self._job_progress_bases(initiative, wake_event)
            lines.extend((f"  {basis}" for basis in bases) if bases else ("  none",))
            lines.append("Ordered runtime-produced effect evidence:")
            lines.extend(
                f"  {index}. {item.name} status={item.status} result={item.runtime_result}"
                for index, item in enumerate(initiative.effects, 1)
            )
            return lines

        async def execute_tool(call: CognitionToolCall) -> CognitionToolResult:
            nonlocal consumed, proposed_disposition, proposed_summary
            nonlocal proposed_readiness, proposed_delay_seconds
            nonlocal proposed_event_type
            nonlocal proposed_progress_update
            if consumed:
                return self._rejected_tool(call.name, "Job outcome request already consumed",
                                           log_prefix="JOBS")
            consumed = True
            try:
                if call.name != REPORT_JOB_OUTCOME_TOOL.name:
                    raise RuntimeError("tool is not available")
                # event_type is schema-required. Accept its omission from older
                # provider/test clients as the equivalent null for non-event modes.
                arguments = json.loads(call.arguments)
                required = {"disposition", "summary", "readiness", "delay_seconds"}
                optional = {"event_type", "progress_update"}
                if (not isinstance(arguments, dict)
                        or not required <= set(arguments)
                        or set(arguments) - required - optional):
                    raise ValueError("invalid report_job_outcome arguments")
                disposition = JobWorkDisposition(arguments["disposition"])
                readiness_value = arguments["readiness"]
                delay_seconds = arguments["delay_seconds"]
                event_value = arguments.get("event_type")
                event_type = (
                    None if event_value is None else JobReadinessEventType(event_value)
                )
                readiness = (
                    None if readiness_value is None
                    else JobContinuationReadiness(readiness_value)
                )
                if disposition is JobWorkDisposition.CONTINUE:
                    if readiness is None:
                        raise ValueError("continue requires readiness")
                    if readiness is JobContinuationReadiness.AFTER_DELAY:
                        if (isinstance(delay_seconds, bool)
                                or not isinstance(delay_seconds, int)
                                or not MIN_JOB_CONTINUATION_DELAY_SECONDS <= delay_seconds <= MAX_JOB_CONTINUATION_DELAY_SECONDS):
                            raise ValueError("after_delay requires a bounded positive integer delay_seconds")
                    elif delay_seconds is not None:
                        raise ValueError(f"{readiness.value} requires null delay_seconds")
                    if readiness is JobContinuationReadiness.WAIT_FOR_EVENT:
                        if event_type is None:
                            raise ValueError("wait_for_event requires event_type")
                    elif event_type is not None:
                        raise ValueError(f"{readiness.value} requires null event_type")
                elif readiness is not None or delay_seconds is not None or event_type is not None:
                    raise ValueError(
                        "terminal outcomes require null readiness, delay_seconds, and event_type"
                    )
                progress_value = arguments.get("progress_update")
                progress_update = self._validate_job_progress_update(
                    progress_value, disposition, initiative, wake_event,
                )
                value = arguments["summary"]
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("summary must be a non-empty string")
                value = value.strip()
                if len(value) > MAX_RUN_SUMMARY_CHARS:
                    raise ValueError(f"summary must be at most {MAX_RUN_SUMMARY_CHARS} characters")
                if not self._job_work_binding_matches(binding, task_binding, goal):
                    raise RuntimeError("exact Job work binding is no longer current")
                proposed_disposition, proposed_summary = disposition, value
                proposed_readiness, proposed_delay_seconds = readiness, delay_seconds
                proposed_event_type = event_type
                proposed_progress_update = progress_update
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, RuntimeError) as error:
                return self._rejected_tool(call.name, str(error), log_prefix="JOBS")
            return CognitionToolResult(json.dumps({
                "status": "accepted",
                "disposition": proposed_disposition.value,
                "commit": "after_outcome_evaluation",
            }, sort_keys=True))

        instructions = "\n\n".join((
            compose_cognition_instructions(
                self.cognition_context(), self.temporal_context(),
                self.temporal_situation(), self.options.startup_prompt,
                self.working_memory.snapshot(),
                goal if self._active_goal is goal else None,
            ), episode.render(), *evidence_lines(),
        ))
        await backend.respond(
            JOB_OUTCOME_EVALUATION_REQUEST, instructions=instructions,
            tools=(REPORT_JOB_OUTCOME_TOOL,), tool_executor=execute_tool,
            refreshed_instructions=lambda: instructions,
        )
        if proposed_disposition is None:
            return JobWorkDisposition.CONTINUE, None, None, None, None, None
        assert proposed_summary is not None
        if proposed_disposition in (
            JobWorkDisposition.COMPLETED, JobWorkDisposition.FAILED,
        ):
            if not self._job_work_binding_matches(binding, task_binding, goal):
                return JobWorkDisposition.CONTINUE, None, None, None, None, None
            terminal = (
                JobRunStatus.COMPLETED
                if proposed_disposition is JobWorkDisposition.COMPLETED
                else JobRunStatus.FAILED
            )
            self.finish_job_run(terminal, proposed_summary)
        elif proposed_progress_update is not None:
            if not self._job_work_binding_matches(binding, task_binding, goal):
                self._clear_stale_job_progress(binding)
                return JobWorkDisposition.CONTINUE, None, None, None, None, None
            progress = self.job_progress
            if progress is None:
                return JobWorkDisposition.CONTINUE, None, None, None, None, None
            updated = progress.increment(proposed_progress_update)
            self._job_progress = updated
            value = next(counter.value for counter in updated.counters
                         if counter.name == proposed_progress_update.counter)
            LOGGER.info(
                "[JOBS] job=JOB%s run=RUN%s progress=%s value=%s basis=%s",
                binding.job.id, binding.run.id, proposed_progress_update.counter,
                value, proposed_progress_update.basis,
            )
        return (proposed_disposition, proposed_summary, proposed_readiness,
                proposed_delay_seconds, proposed_event_type, proposed_progress_update)

    @staticmethod
    def _job_progress_bases(
        initiative: InitiativeOutcome, wake_event: JobWakeEvent | None,
    ) -> tuple[str, ...]:
        bases: list[str] = []
        if wake_event is not None:
            bases.append("wake_event")
        bases.extend(f"acquisition_{index}" for index, item in
                     enumerate(initiative.acquisitions, 1) if item.status == "applied")
        bases.extend(f"effect_{index}" for index, item in
                     enumerate(initiative.effects, 1) if item.status == "applied")
        return tuple(bases)

    def _validate_job_progress_update(
        self, value: object, disposition: JobWorkDisposition,
        initiative: InitiativeOutcome, wake_event: JobWakeEvent | None,
    ) -> JobProgressUpdate | None:
        if value is None:
            return None
        if disposition is not JobWorkDisposition.CONTINUE:
            raise ValueError("terminal outcomes require null progress_update")
        if not isinstance(value, dict) or set(value) != {"counter", "basis"}:
            raise ValueError("progress_update requires exactly counter and basis")
        counter = validate_counter_name(value["counter"])
        basis = value["basis"]
        if not isinstance(basis, str) or basis not in JOB_PROGRESS_BASES:
            raise ValueError("unsupported progress evidence basis")
        if basis not in self._job_progress_bases(initiative, wake_event):
            raise ValueError("progress evidence basis is unavailable in this episode")
        progress = self.job_progress
        if progress is None:
            raise ValueError("exact Job progress binding is unavailable")
        # Validate catalog/value bounds without mutating the immutable snapshot.
        progress.increment(JobProgressUpdate(counter, basis))
        return JobProgressUpdate(counter, basis)

    def temporal_context(self) -> TemporalContext:
        """Build fresh local wall-clock grounding for one cognition boundary."""
        instant = self._aware_wall_clock()
        return TemporalContext(
            instant.astimezone(self._timezone), self._timezone_name
        )

    def _aware_wall_clock(self) -> datetime:
        """Read and validate the application-owned model-facing clock."""
        instant = self._wall_clock()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("wall clock must return an offset-aware datetime")
        return instant

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
        if self._current_task_binding is not None:
            raise RuntimeError("cannot set a standalone goal while a Task is current")
        if self._active_goal is not None:
            raise RuntimeError("an active goal already exists")
        return self._create_active_goal(description)

    def _create_active_goal(self, description: object) -> ActiveGoal:
        """Create and install a normal session-local ActiveGoal."""
        normalized = validate_goal_description(description)
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
        if (
            self._current_task_binding is not None
            and self._current_task_binding.active_goal is not None
        ):
            raise RuntimeError("cannot resolve a Task-bound active goal directly")
        if self._active_goal is None:
            raise RuntimeError("no active goal exists")
        previous = self._active_goal
        self._active_goal = None
        self._active_goal_started_monotonic = None
        self.temporal.cancel("goal_changed")
        LOGGER.info("[GOAL] goal=G%s status=%s", previous.id, outcome)
        return previous

    def clear_goal(self) -> bool:
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Clearing a goal requires a running application")
        if (
            self._current_task_binding is not None
            and self._current_task_binding.active_goal is not None
        ):
            raise RuntimeError("cannot clear a Task-bound active goal directly")
        previous = self._active_goal
        cleared = previous is not None
        self._active_goal = None
        self._active_goal_started_monotonic = None
        if cleared:
            self.temporal.cancel("goal_changed")
            LOGGER.info("[GOAL] goal=G%s status=cleared", previous.id)
        return cleared

    def start_task(self, task: Task) -> Task:
        """Install one pending Task as the application's current running work."""
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Starting a Task requires a running application")
        if not isinstance(task, Task):
            raise TypeError("task must be a Task")
        if self._current_task_binding is not None:
            raise RuntimeError("a current Task already exists")
        if self._active_goal is not None:
            raise RuntimeError("an unrelated active goal already exists")
        if task.status is not TaskStatus.PENDING:
            raise ValueError("Task must be pending")

        running = task.transition_to(TaskStatus.RUNNING)
        goal = (
            None
            if running.goal is None
            else self._create_active_goal(running.goal.description)
        )
        self._current_task_binding = _CurrentTaskBinding(running, goal)
        LOGGER.info(
            "[TASK] task=%s status=running goal=%s",
            running.id,
            "none" if goal is None else f"G{goal.id}",
        )
        return running

    @staticmethod
    def _task_resource_owner(task: Task) -> ResourceOwner:
        """Derive stable semantic resource ownership from the Task UUID."""
        return ResourceOwner("task", str(task.id))

    def acquire_task_resource(self, resource: ResourceKey) -> ResourceLease:
        """Acquire one resource for the current running Task without waiting."""
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Acquiring a Task resource requires a running application")
        binding = self._current_task_binding
        if binding is None:
            raise RuntimeError("no current Task exists")
        if binding.task.status is not TaskStatus.RUNNING:
            raise RuntimeError("current Task must be running")
        self._validate_current_task_binding(binding)
        return self.resources.acquire(resource, self._task_resource_owner(binding.task))

    def release_task_resource(self, lease: ResourceLease) -> None:
        """Release an exact active lease owned by the current Task."""
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Releasing a Task resource requires a running application")
        binding = self._current_task_binding
        if binding is None:
            raise RuntimeError("no current Task exists")
        if not isinstance(lease, ResourceLease):
            raise TypeError("lease must be a ResourceLease")
        owner = self._task_resource_owner(binding.task)
        if lease.owner != owner:
            raise RuntimeError("resource lease is not owned by the current Task")
        self.resources.release(lease)

    def finish_task(self, status: TaskStatus) -> Task:
        """End the current Task in one explicitly selected terminal state."""
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Finishing a Task requires a running application")
        if not isinstance(status, TaskStatus):
            raise TypeError("status must be a TaskStatus")
        if status not in (
            TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED
        ):
            raise ValueError("status must be completed, failed, or stopped")
        binding = self._current_task_binding
        if binding is None:
            raise RuntimeError("no current Task exists")

        continuation = self._job_continuation
        if continuation is not None and continuation.task_id == binding.task.id:
            self._clear_job_continuation(f"task_{status.value}")

        terminal = binding.task.transition_to(status)
        self._refresh_current_job_task(terminal)
        self._release_current_task_binding(binding)
        LOGGER.info("[TASK] task=%s status=%s", terminal.id, terminal.status.value)
        return terminal

    def pause_task(self) -> Task:
        """Suspend the current Task's runtime intention while retaining ownership."""
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Pausing a Task requires a running application")
        binding = self._current_task_binding
        if binding is None:
            raise RuntimeError("no current Task exists")
        if binding.task.status is not TaskStatus.RUNNING:
            raise RuntimeError("current Task must be running")

        self._validate_current_task_binding(binding)
        paused = binding.task.transition_to(TaskStatus.PAUSED)
        self._release_task_resources(binding)
        self._release_task_active_goal(binding)
        self._current_task_binding = _CurrentTaskBinding(paused, None)
        self._refresh_current_job_task(paused)
        LOGGER.info("[TASK] task=%s status=paused", paused.id)
        return paused

    def resume_task(self) -> Task:
        """Reactivate the current paused Task with a fresh runtime intention."""
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Resuming a Task requires a running application")
        binding = self._current_task_binding
        if binding is None:
            raise RuntimeError("no current Task exists")
        if binding.task.status is not TaskStatus.PAUSED:
            raise RuntimeError("current Task must be paused")

        self._validate_current_task_binding(binding)
        running = binding.task.transition_to(TaskStatus.RUNNING)
        goal = (
            None
            if running.goal is None
            else self._create_active_goal(running.goal.description)
        )
        self._current_task_binding = _CurrentTaskBinding(running, goal)
        self._refresh_current_job_task(running)
        LOGGER.info(
            "[TASK] task=%s status=running goal=%s",
            running.id,
            "none" if goal is None else f"G{goal.id}",
        )
        return running

    def stop_task(self) -> Task:
        """Semantically stop and release the current running or paused Task."""
        return self.finish_task(TaskStatus.STOPPED)

    def _refresh_current_job_task(self, task: Task) -> None:
        """Keep the volatile Job view aligned with Task lifecycle snapshots."""
        binding = self._current_job_run
        if binding is not None:
            if binding.task.id != task.id:
                raise RuntimeError("current JobRun Task binding is inconsistent")
            self._current_job_run = CurrentJobRun(binding.job, binding.run, task)

    def _validate_current_task_binding(self, binding: _CurrentTaskBinding) -> None:
        """Fail closed unless Task ownership matches the exact active intention."""
        goal = binding.active_goal
        if binding.task.status is TaskStatus.PAUSED:
            if goal is not None or self._active_goal is not None:
                raise RuntimeError("current Task ActiveGoal binding is inconsistent")
        elif binding.task.goal is None:
            if goal is not None or self._active_goal is not None:
                raise RuntimeError("current Task ActiveGoal binding is inconsistent")
        elif goal is None or self._active_goal is not goal:
            raise RuntimeError("current Task ActiveGoal binding is inconsistent")

        marker = self._active_goal_started_monotonic
        if (goal is None and marker is not None) or (
            goal is not None and (marker is None or marker[0] is not goal)
        ):
            raise RuntimeError("current Task ActiveGoal binding is inconsistent")

    def _release_task_active_goal(self, binding: _CurrentTaskBinding) -> None:
        """Release only the exact runtime intention owned by a Task binding."""
        self._validate_current_task_binding(binding)
        goal = binding.active_goal
        if goal is None:
            return
        self._active_goal = None
        self._active_goal_started_monotonic = None
        self.temporal.cancel("goal_changed")

    def _release_current_task_binding(self, binding: _CurrentTaskBinding) -> None:
        """Remove volatile Task ownership without changing its domain snapshot."""
        self._release_task_resources(binding)
        self._release_task_active_goal(binding)
        self._current_task_binding = None

    def _release_task_resources(self, binding: _CurrentTaskBinding) -> None:
        """Release all leases belonging to the binding's stable Task identity."""
        self.resources.release_all(self._task_resource_owner(binding.task))

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
                voltage = self.hardware.read_battery_voltage_v()
                power = PowerState(
                    battery_voltage_v=voltage,
                    observed_at=(
                        self._aware_wall_clock() if self.state is LifecycleState.RUNNING
                        else None
                    ),
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
            if self._cognition_backend is not None:
                backend = self._cognition_backend
                LOGGER.info(
                    "[COGNITION] backend=%s preparation=started", backend.identifier
                )
                try:
                    await backend.prepare()
                except CognitionError:
                    LOGGER.info(
                        "[COGNITION] backend=%s preparation=failed status=degraded",
                        backend.identifier,
                    )
                else:
                    LOGGER.info(
                        "[COGNITION] backend=%s preparation=ready", backend.identifier
                    )
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
            try:
                self._close_persistent_memory()
            except BaseException:
                LOGGER.exception("[MEMORY] cleanup_failed")
            try:
                self._close_job_store()
            except BaseException:
                LOGGER.exception("[JOBS] cleanup_failed")
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
        if self._job_continuation_controller is not None:
            self._job_continuation_controller.start()
            LOGGER.info(
                "[JOBS] continuation=ready heartbeat_s=%s max_auto_steps=%s",
                self.options.jobs_heartbeat_seconds,
                self.options.jobs_max_auto_steps,
            )
        if self._scheduled_job_controller is not None:
            self._scheduled_job_controller.start()
            LOGGER.info("[JOBS] scheduler=ready poll_s=%s",
                        self.options.jobs_scheduler_poll_seconds)
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
        if self._scheduled_job_controller is not None:
            try:
                await self._scheduled_job_controller.stop()
                LOGGER.info("[JOBS] scheduler=stopped")
            except BaseException as error:
                failure = error
        if self._job_continuation_controller is not None:
            try:
                await self._job_continuation_controller.stop()
            except BaseException as error:
                failure = error
        try:
            await self.voice.stop()
        except BaseException as error:
            failure = error
        try:
            await self._stop_operator_cognition()
        except BaseException as error:
            failure = failure or error
        try:
            await self._stop_job_work()
        except BaseException as error:
            failure = failure or error
        self._clear_job_continuation("shutdown")
        self._job_progress = None
        try:
            await self.temporal.stop()
        except BaseException as error:
            failure = error
        binding = self._current_task_binding
        if binding is not None:
            # Session shutdown drops coordination only; the running or paused Task
            # snapshot is deliberately not given a semantic terminal state.
            try:
                self._release_task_resources(binding)
            except BaseException as error:
                failure = failure or error
            try:
                self._release_task_active_goal(binding)
            except BaseException as error:
                failure = failure or error
            self._current_task_binding = None
        if self._current_job_run is not None:
            current = self._current_job_run
            LOGGER.info(
                "[JOBS] job=JOB%s run=RUN%s task=%s status=detached",
                current.job.id, current.run.id, current.task.id,
            )
            self._current_job_run = None
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
        except BaseException as error:
            failure = failure or error
        try:
            self._close_persistent_memory()
        except BaseException as error:
            failure = failure or error
        try:
            self._close_job_store()
        except BaseException as error:
            failure = failure or error
        LOGGER.info("[APP] stopped")
        if failure is not None:
            raise failure

    def _close_persistent_memory(self) -> None:
        """Make the application's single best-effort ownership close attempt."""
        if self.persistent_memory is None or self._persistent_memory_closed:
            return
        self._persistent_memory_closed = True
        self.persistent_memory.close()

    async def _stop_job_work(self) -> None:
        """Cancel and join explicit Job cognition before volatile Task cleanup."""
        task = self._active_job_work_task
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._active_job_work_task is task:
            self._active_job_work_task = None

    def _close_job_store(self) -> None:
        """Close Job persistence without changing any durable run state."""
        if self.jobs is None or self._job_store_closed:
            return
        self._job_store_closed = True
        self.jobs.close()

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
        frame = self._capture_camera_frame_for_owner(CAMERA_CAPTURE_OWNER)
        LOGGER.info(
            "[CAMERA] capture width=%s height=%s media_type=%s bytes=%s",
            frame.width, frame.height, frame.media_type, len(frame.data),
        )
        return frame

    def _capture_camera_frame_for_owner(self, owner: ResourceOwner) -> CameraFrame:
        """Capture one frame under an exact, short-lived camera lease."""
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Camera capture requires a running application")
        if self.camera_backend is None:
            raise RuntimeError("No camera backend is configured")
        try:
            lease = self.resources.acquire(CAMERA_RESOURCE, owner)
        except BaseException as error:
            self.observability.increment("camera_failures")
            self.observability.increment("resource_failures")
            self.observability.event("camera", "capture", "failed", severity="error",
                                     error=type(error).__name__)
            raise
        try:
            frame = self.camera_backend.capture_frame()
            self.observability.increment("camera_captures")
            self.observability.event("camera", "capture", "completed", metadata={
                "backend": self.camera_backend.identifier,
                "bytes": len(frame.data), "width": frame.width, "height": frame.height,
            })
            return frame
        except BaseException as error:
            self.observability.increment("camera_failures")
            self.observability.event("camera", "capture", "failed", severity="error",
                                     error=type(error).__name__)
            raise
        finally:
            self.resources.release(lease)

    def _visual_perception_resource_owner(
        self, expected_goal: ActiveGoal | None, *, autonomous: bool,
    ) -> ResourceOwner:
        binding = self._current_task_binding
        if (
            autonomous
            and binding is not None
            and binding.task.status is TaskStatus.RUNNING
            and binding.active_goal is not None
            and binding.active_goal is expected_goal
            and self._active_goal is expected_goal
        ):
            return self._task_resource_owner(binding.task)
        return VISUAL_PERCEPTION_OWNER

    async def request_cognition(
        self, message: str, *, interaction: InteractionContext | None = None,
        source: str | None = None,
    ) -> str:
        """Run one finite operator attention episode."""
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Cognition requires a running application")
        if self._cognition_backend is None:
            raise RuntimeError("No cognition backend is configured")
        if not message or not message.strip():
            raise ValueError("Cognition message must be non-empty")
        if interaction is not None and not (
            interaction.channel in (
                InteractionChannel.CONSOLE, InteractionChannel.VOICE
            )
            and interaction.mode == InteractionMode.DIALOGUE
            and interaction.initiator == InteractionInitiator.OPERATOR
            and interaction.response_expected is True
        ):
            raise ValueError(
                "Explicit operator interaction must be supported operator dialogue "
                "with a response expected"
            )
        backend = self._cognition_backend
        trigger_source = (
            interaction.channel.value if interaction is not None
            else source or OPERATOR_SOURCE.get()
        )
        episode = await self.episode_coordinator.start_operator(
            trigger_source, OPERATOR_EPISODE_CONCERN
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
            return await self._run_operator_episode(
                message, backend, episode, interaction
            )
        finally:
            if self._active_operator_cognition_task is task:
                self._active_operator_cognition_task = None

    async def _run_operator_episode(
        self, message: str, backend: TextCognitionBackend,
        episode: AttentionEpisode, interaction: InteractionContext | None,
    ) -> str:
        """Execute an episode while retaining its interaction-layer identity."""
        prior_memory = self.working_memory.snapshot()
        tool_outcomes: list[WorkingMemoryToolOutcome] = []
        acquisitions: list[InitiativeAcquisitionOutcome] = []
        acquisition_requests: dict[tuple[str, str], CognitionToolResult] = {}
        stage_name = "initial"
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
                delivery_destinations = self._operator_delivery_routes.destinations
                tools = self._operator_episode_tools(
                    len(acquisitions), delivery_destinations
                )

                async def execute_tool(call: CognitionToolCall) -> CognitionToolResult:
                    nonlocal acquired, capability_consumed
                    if self.state is not LifecycleState.RUNNING:
                        raise asyncio.CancelledError
                    if capability_consumed:
                        LOGGER.info(
                            "[COGNITION] episode=E%s tool=%s class=unavailable "
                            "status=requested", episode.id, call.name,
                        )
                        result = self._rejected_tool(
                            call.name, "operator capability request already consumed"
                        )
                        LOGGER.info(
                            "[COGNITION] episode=E%s tool=%s class=unavailable "
                            "status=rejected", episode.id, call.name,
                        )
                        return result
                    capability_consumed = True
                    if not any(tool.name == call.name for tool in tools):
                        LOGGER.info(
                            "[COGNITION] episode=E%s tool=%s class=unavailable "
                            "status=requested", episode.id, call.name,
                        )
                        result = self._rejected_tool(call.name, "tool is not available")
                        LOGGER.info(
                            "[COGNITION] episode=E%s tool=%s class=unavailable "
                            "status=rejected", episode.id, call.name,
                        )
                        tool_outcomes.append(
                            WorkingMemoryToolOutcome(call.name, result.output)
                        )
                        return result
                    if call.name in self._acquisition_tool_names():
                        number = len(acquisitions) + 1
                        LOGGER.info(
                            "[ATTENTION] episode=E%s acquisition=%s/2 tool=%s status=requested",
                            episode.id, number, call.name,
                        )
                        acquisition_arguments: object = call.arguments
                        if call.name == INSPECT_RUN_HISTORY_TOOL.name:
                            try:
                                acquisition_arguments = self._normalize_run_history_arguments(call)
                            except (json.JSONDecodeError, TypeError, ValueError):
                                pass
                        acquisition_key = (call.name, acquisition_arguments)
                        if acquisition_key in acquisition_requests:
                            # Re-present already accumulated evidence without
                            # repeating I/O or consuming another acquisition.
                            result = acquisition_requests[acquisition_key]
                            return result
                        elif call.name == INSPECT_SELF_TOOL.name:
                            result, inspection = self._execute_self_inspection(call)
                            perception = None
                        elif call.name == OBSERVE_SCENE_TOOL.name:
                            result, perception = await self._execute_visual_perception(call)
                            inspection = None
                        elif call.name == INSPECT_RUN_HISTORY_TOOL.name:
                            result = self._execute_run_history_inspection(
                                call, episode_id=episode.id)
                            inspection = perception = None
                        else:
                            result = self._execute_memory_recall(call)
                            inspection = perception = None
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
                        LOGGER.info(
                            "[COGNITION] episode=E%s tool=%s class=effect status=requested",
                            episode.id, call.name,
                        )
                        if call.name == "deliver_message":
                            result = await self._execute_deliver_message(
                                call, tools, delivery_destinations,
                                episode.trigger_source, episode_id=episode.id,
                            )
                        elif call.name == REMEMBER_TOOL.name:
                            result = self._execute_memory_admission(
                                call, message, episode.trigger_source,
                                episode_id=episode.id,
                            )
                        else:
                            result = await self._execute_cognition_tool(
                                call, expected_goal=grounded_goal
                            )
                        LOGGER.info(
                            "[COGNITION] episode=E%s tool=%s class=effect status=%s",
                            episode.id, call.name, self._tool_result_status(result),
                        )
                    tool_outcomes.append(WorkingMemoryToolOutcome(call.name, result.output))
                    return result

                stage_name = "initial" if stage == 0 else f"post_acquisition_{stage}"
                LOGGER.info(
                    "[COGNITION] episode=E%s stage=%s source=%s backend=%s request=started",
                    episode.id, stage_name, episode.trigger_source, backend.identifier,
                )
                response = await backend.respond(
                    message,
                    instructions=self._operator_episode_instructions(
                        episode, message, prior_memory, acquisitions, interaction,
                        delivery_destinations,
                    ),
                    tools=tools,
                    tool_executor=execute_tool if tools else None,
                    refreshed_instructions=lambda: self._operator_episode_instructions(
                        episode, message, prior_memory, acquisitions, interaction,
                        delivery_destinations,
                    ),
                )
                LOGGER.info(
                    "[COGNITION] episode=E%s stage=%s source=%s backend=%s "
                    "request=completed response_chars=%s",
                    episode.id, stage_name, episode.trigger_source,
                    backend.identifier, len(response),
                )
                if not acquired:
                    break
        except asyncio.CancelledError:
            await self._finish_operator_episode(episode, "cancelled")
            raise
        except Exception:
            LOGGER.warning(
                "[COGNITION] episode=E%s stage=%s source=%s backend=%s request=failed",
                episode.id, stage_name, episode.trigger_source, backend.identifier,
            )
            await self._finish_operator_episode(episode, "error")
            raise
        if self.state is not LifecycleState.RUNNING:
            await self._finish_operator_episode(episode, "cancelled")
            raise asyncio.CancelledError
        observations: list[WorkingMemoryObservation] = []
        power = self._runtime_state.power
        if power.battery_voltage_v is not None and power.observed_at is not None:
            observations.append(WorkingMemoryObservation(
                "power", self.hardware.identifier, power.observed_at,
                (("battery_voltage_v", f"{power.battery_voltage_v:.3f}"),),
            ))
        for acquisition in acquisitions:
            if acquisition.inspection_result is not None:
                result = acquisition.inspection_result
                observations.append(WorkingMemoryObservation(
                    "self_inspection", result.area, result.observed_at,
                    tuple((fact.name, fact.value) for fact in result.facts[:16]),
                ))
            elif acquisition.perception_result is not None:
                result = acquisition.perception_result
                observations.append(WorkingMemoryObservation(
                    "visual_interpretation", self.camera_backend.identifier,
                    result.observed_at,
                    (("focus", result.focus), ("description", result.description)),
                ))
        self.working_memory.append(
            message, response, tool_outcomes,
            completed_at=self._aware_wall_clock(), observations=observations,
        )
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
        self, message: str, *, interaction: InteractionContext | None = None,
        source: str = "operator",
    ) -> str:
        """Route one typed or spoken operator utterance through cognition."""
        resolved_source = interaction.channel.value if interaction else source
        token = OPERATOR_SOURCE.set(resolved_source)
        try:
            return await self.request_cognition(message, interaction=interaction)
        finally:
            OPERATOR_SOURCE.reset(token)

    def _operator_episode_tools(
        self, acquisitions_used: int,
        delivery_destinations: Sequence[OperatorDeliveryDestination] | None = None,
    ) -> tuple[CognitionToolDefinition, ...]:
        destinations = tuple(delivery_destinations or ())
        tools = self.cognition_tools()
        if destinations:
            tools = (*tools, deliver_message_tool(destinations))
        if acquisitions_used >= 2:
            return tuple(tool for tool in tools
                         if tool.name not in self._acquisition_tool_names())
        return tools

    def _operator_episode_instructions(
        self, episode: AttentionEpisode, message: str, working_memory,
        acquisitions: list[InitiativeAcquisitionOutcome],
        interaction: InteractionContext | None,
        delivery_destinations: Sequence[OperatorDeliveryDestination] = (),
    ) -> str:
        remaining = 2 - len(acquisitions)
        lines = [
            compose_cognition_instructions(
                self.cognition_context(), self.temporal_context(), self.temporal_situation(),
                self.options.startup_prompt,
                working_memory, self._active_goal,
            ),
        ]
        if interaction is not None:
            lines.append(interaction.render())
            lines.append(render_dialogue_policy(interaction))
        if delivery_destinations:
            lines.append("\n".join((
                "Available operator delivery destinations",
                *(f"  {item.name}: {item.description}"
                  for item in delivery_destinations),
            )))
            lines.append("\n".join((
                "Operator delivery policy",
                "These available effects are destinations to which the operator may explicitly request content be delivered.",
                "Use deliver_message only when the current request explicitly requests or clearly authorizes delivery.",
                "The destination is semantic and runtime-owned; do not invent account, recipient, transport, or credential identifiers.",
                "Write a self-contained message suitable for its destination; the console is plain text and has no assumed Markdown rendering.",
                "Delivery is separate from the current dialogue response and does not change that response's medium.",
                "Do not claim success until the runtime tool result reports status=applied.",
                "Applied means the configured destination accepted the message, not that a human read, saw, or acknowledged it.",
            )))
        lines.extend([
            episode.render(),
            "Operator episode policy",
        ])
        if interaction is not None:
            lines.append(
                "This context is authoritative for the current communication "
                "setting. It describes this exchange and does not change capability "
                "permissions or attention budgets. response_expected means this "
                "interaction expects a direct response from the assistant to the initiating "
                "operator communication; it does not mean the operator is required "
                "to reply afterward."
            )
        lines.extend([
            "The original operator request is the current request; do not copy it into episode identity.",
            f"  acquisitions_used: {len(acquisitions)}",
            f"  acquisitions_remaining: {remaining}",
            "At most one offered capability may be requested in this cognition stage.",
        ])
        if self._memory_recall is not None:
            lines.append(
                "Persistent memory is not automatically in context. recall_memory is a "
                "deliberate exact-name acquisition; its evidence is historical and may be "
                "stale, and ambiguous matches must remain explicit."
            )
            lines.append(
                "Durable memory admission is selective. Use remember only for durable "
                "facts, preferences, or simple relationships directly stated by the "
                "operator in the current utterance and likely useful beyond this session. "
                "Do not persist guesses, recalled information, transient state, ordinary "
                "conversation, jokes/sarcasm, or assistant-generated conclusions. The "
                "subject must already exist in persistent memory."
            )
            lines.append(
                "Persistent-memory acknowledgement policy: only the authoritative "
                "remember result for THIS turn proves what happened. status=applied "
                "with admission=created permits saying a new durable memory was saved. "
                "status=applied with admission=duplicate means it was already stored; "
                "do not imply this turn created or just saved a record. status=rejected "
                "means nothing was written; clearly avoid any success claim and, when "
                "useful, explain only its bounded reason. If remember was not called or "
                "there is no authoritative remember result this turn, never claim durable "
                "memory changed. Working memory, operator intent, and prior turns are not "
                "proof of a persistent write."
            )
        if self._run_history_evidence is not None:
            lines.append(self._run_history_grounding())
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
        tools: tuple[CognitionToolDefinition, ...] = (),
        notification_interaction: InteractionContext | None = None,
        previous_work_summary: str | None = None,
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
            "\n\nRead-only acquisition capabilities offered in this request may be used for "
            "bounded missing information. This episode permits at most two acquisition "
            "attempts across separate cognition requests. Use one only when materially "
            "relevant to the active goal; do not acquire information merely because "
            "a capability exists, retry an acquisition, or request the same information. "
            "Every acquisition must serve this SAME concern and goal. You may request at "
            "most one capability in this request."
            if capabilities_available else ""
        )
        history_guidance = (
            "\n\n" + self._run_history_grounding()
            if self._run_history_evidence is not None and capabilities_available else ""
        )
        sections = self._notification_sections(tools, notification_interaction)
        notification = "" if not sections else "\n\n" + "\n\n".join(sections)
        continuity = render_job_continuity(previous_work_summary)
        continuity_section = "" if continuity is None else f"\n\n{continuity}"
        progress = self.job_progress
        progress_section = (
            f"\n\n{progress.render()}" if progress is not None
            and stimulus.kind == "job_run_work" else ""
        )
        return (
            f"{context}{notification}\n\n{episode.render()}\n\n"
            f"{stimulus.render(actions_enabled=capabilities_available)}"
            f"{continuity_section}{progress_section}{inspection_guidance}"
            f"{history_guidance}{sequencing}"
        )

    @staticmethod
    def _run_history_grounding() -> str:
        return (
            "Run history is deliberate, content-filtered operational evidence, not "
            "automatically remembered semantic knowledge. An inspect_run_history result is "
            "not evidence that the information was remembered. If the operator asks whether "
            "you remember something and it is available only from run history, do not answer "
            "yes, I remember; explicitly say you do not have it as remembered knowledge, but "
            "the run record shows it. Persistent-memory evidence may independently support a "
            "memory claim. Previous evidence may be stale; "
            "fresh Runtime context is authoritative for current state. The current selector "
            "is only a partial snapshot persisted so far. A stored started status alone does "
            "not prove running, crashed, or abandoned. Attribute acquired facts to the run "
            "record, do not reconstruct withheld content, and inspect history only when "
            "materially relevant."
        )

    async def _request_initiative(
        self, stimulus: AttentionStimulus, episode: AttentionEpisode | None = None,
        *, evaluate_goal_outcome: bool = True, job_work: bool = False,
        previous_work_summary: str | None = None,
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
        tools = self._initiative_tools_for_episode(job_work=job_work)
        sink = self._operator_message_sink
        notification_interaction = (
            resolve_notification_route(sink.channel)
            if sink is not None and any(tool.name == ADDRESS_OPERATOR_TOOL.name for tool in tools)
            else None
        )
        capabilities_available = bool(tools)
        instructions = self._attention_instructions(
            stimulus, episode, prior_memory, capabilities_available=capabilities_available,
            expected_goal=expected_goal,
            tools=tools, notification_interaction=notification_interaction,
            previous_work_summary=previous_work_summary,
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
            capability_class = (
                "acquisition" if call.name in self._acquisition_tool_names() else "effect"
            )
            LOGGER.info(
                "[INITIATIVE] episode=E%s goal=G%s tool=%s class=%s status=requested",
                episode.id, expected_goal.id, call.name, capability_class,
            )
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
            elif call.name == RECALL_MEMORY_TOOL.name:
                LOGGER.info(
                    "[ATTENTION] episode=E%s acquisition=1/%s tool=%s status=requested",
                    episode.id, MAX_AUTONOMOUS_ACQUISITIONS_PER_EPISODE, call.name,
                )
                result = self._execute_memory_recall(
                    call, expected_goal=expected_goal, autonomous=True
                )
            elif call.name == INSPECT_RUN_HISTORY_TOOL.name:
                LOGGER.info(
                    "[ATTENTION] episode=E%s acquisition=1/%s tool=%s status=requested",
                    episode.id, MAX_AUTONOMOUS_ACQUISITIONS_PER_EPISODE, call.name,
                )
                result = self._execute_run_history_inspection(
                    call, expected_goal=expected_goal, autonomous=True,
                    episode_id=episode.id)
            else:
                action = call.name
                if self._active_goal is not expected_goal:
                    result = self._rejected_tool(
                        call.name, "attention episode's bound goal is no longer current",
                        log_prefix="INITIATIVE",
                    )
                else:
                    result = await self._execute_initiative_tool(
                        call, available=self._initiative_tools_for_episode(
                            job_work=job_work
                        ), notification_interaction=notification_interaction
                    )
            try:
                result_status = json.loads(result.output).get("status", "rejected")
            except (json.JSONDecodeError, AttributeError):
                result_status = "rejected"
            if call.name in self._acquisition_tool_names():
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
            LOGGER.info(
                "[INITIATIVE] episode=E%s goal=G%s tool=%s class=%s status=%s",
                episode.id, expected_goal.id, call.name, capability_class, result_status,
            )
            return result

        LOGGER.info(
            "[INITIATIVE] episode=E%s goal=G%s stage=initial backend=%s "
            "request=started capabilities=%s",
            episode.id, expected_goal.id, backend.identifier,
            "enabled" if capabilities_available else "disabled",
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
                        tools=tools, notification_interaction=notification_interaction,
                        previous_work_summary=previous_work_summary,
                    )
                ) if tools else None,
            )
        except Exception:
            LOGGER.warning(
                "[INITIATIVE] episode=E%s goal=G%s stage=initial backend=%s request=failed",
                episode.id, expected_goal.id, backend.identifier,
            )
            raise
        LOGGER.info(
            "[INITIATIVE] episode=E%s goal=G%s stage=initial backend=%s "
            "request=completed response_chars=%s",
            episode.id, expected_goal.id, backend.identifier, len(response),
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
                notification_interaction, job_work=job_work,
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
            and self._continuation_tools_for_episode(
                effects[0].name, job_work=job_work
            )
        ):
            continuation_completed, continuation_effect = await self._request_continuation(
                stimulus, episode, expected_goal, prior_memory, effects[0],
                tuple(acquisitions),
                notification_interaction, job_work=job_work,
            )
            if continuation_effect is not None:
                effects.append(continuation_effect)
        if (
            continuation_completed
            and evaluate_goal_outcome
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
        return InitiativeOutcome(
            response, action, action_status, tuple(acquisitions), tuple(effects)
        )

    def _acquisition_followup_instructions(
        self, followup: AcquisitionFollowupStimulus,
        stimulus: AttentionStimulus, episode: AttentionEpisode,
        expected_goal: ActiveGoal, working_memory,
        tools: tuple[CognitionToolDefinition, ...] = (),
        notification_interaction: InteractionContext | None = None,
    ) -> str:
        return "\n\n".join((
            compose_cognition_instructions(
                self.cognition_context(), self.temporal_context(), self.temporal_situation(),
                self.options.startup_prompt, working_memory,
                expected_goal if self._active_goal is expected_goal else None,
            ), *self._notification_sections(tools, notification_interaction),
            episode.render(), stimulus.render(actions_enabled=None), followup.render(),
        ))

    @staticmethod
    def _notification_sections(tools, interaction):
        """Describe notification delivery only where its effect is projected."""
        if interaction is None or not any(
            tool.name == ADDRESS_OPERATOR_TOOL.name for tool in tools
        ):
            return ()
        return (render_notification_context(interaction),
                render_notification_policy(interaction))

    async def _request_acquisition_followup(
        self, stimulus: AttentionStimulus, episode: AttentionEpisode,
        expected_goal: ActiveGoal, prior_memory,
        acquisitions: list[InitiativeAcquisitionOutcome],
        notification_interaction: InteractionContext | None = None,
        *, job_work: bool = False,
    ) -> tuple[bool, InitiativeEffectOutcome | None]:
        backend = self._cognition_backend
        assert backend is not None
        # This helper is deliberately finite: one decision after acquisition #1,
        # followed by exactly one effect-only decision if #2 was attempted.
        followup = AcquisitionFollowupStimulus(tuple(acquisitions))
        tools = (*self.acquisition_tools(), *self._effect_tools_for_episode(
            job_work=job_work
        ))
        log_prefix = "ACQUISITION"
        action = status = result_text = None
        second_acquisition: InitiativeAcquisitionOutcome | None = None
        consumed = False
        LOGGER.info(
            "[ACQUISITION] episode=E%s goal=G%s continuation=post_acquisition_1 "
            "backend=%s request=started",
            episode.id, expected_goal.id, backend.identifier,
        )

        async def execute_tool(call: CognitionToolCall) -> CognitionToolResult:
            nonlocal action, status, result_text, consumed, second_acquisition
            if consumed:
                return self._rejected_tool(
                    call.name, "acquisition follow-up already consumed",
                    log_prefix=log_prefix,
                )
            consumed = True
            available = (*self.acquisition_tools(), *self._effect_tools_for_episode(
                job_work=job_work
            ))
            inspection = perception = None
            if call.name in self._acquisition_tool_names():
                LOGGER.info(
                    "[ATTENTION] episode=E%s acquisition=2/%s tool=%s status=requested",
                    episode.id, MAX_AUTONOMOUS_ACQUISITIONS_PER_EPISODE, call.name,
                )
            else:
                LOGGER.info(
                    "[ACQUISITION] episode=E%s goal=G%s tool=%s class=effect "
                    "status=requested", episode.id, expected_goal.id, call.name,
                )
            if (self.state is not LifecycleState.RUNNING
                    or self._active_goal is not expected_goal
                    or not any(tool.name == call.name for tool in available)):
                result = self._rejected_tool(
                    call.name, "capability is not available",
                    log_prefix=log_prefix,
                )
            elif call.name in self._acquisition_tool_names():
                if call.name == INSPECT_SELF_TOOL.name:
                    result, inspection = self._execute_self_inspection(
                        call, expected_goal=expected_goal, autonomous=True
                    )
                elif call.name == OBSERVE_SCENE_TOOL.name:
                    result, perception = await self._execute_visual_perception(
                        call, expected_goal=expected_goal, autonomous=True
                    )
                elif call.name == INSPECT_RUN_HISTORY_TOOL.name:
                    result = self._execute_run_history_inspection(
                        call, expected_goal=expected_goal, autonomous=True,
                        episode_id=episode.id)
                else:
                    result = self._execute_memory_recall(
                        call, expected_goal=expected_goal, autonomous=True
                    )
            else:
                action = call.name
                result = await self._execute_initiative_tool(
                    call, available=available, log_prefix=log_prefix,
                    notification_interaction=notification_interaction,
                )
            result_text = result.output
            try:
                status = json.loads(result.output).get("status", "rejected")
            except (json.JSONDecodeError, AttributeError):
                status = "rejected"
            if call.name in self._acquisition_tool_names():
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
            if call.name not in self._acquisition_tool_names():
                LOGGER.info(
                    "[ACQUISITION] episode=E%s goal=G%s tool=%s class=effect "
                    "status=%s", episode.id, expected_goal.id, call.name, status,
                )
            return result

        try:
            await backend.respond(
                ACQUISITION_FOLLOWUP_REQUEST,
                instructions=self._acquisition_followup_instructions(
                    followup, stimulus, episode, expected_goal, prior_memory,
                    tools, notification_interaction,
                ), tools=tools, tool_executor=execute_tool if tools else None,
                refreshed_instructions=lambda: self._acquisition_followup_instructions(
                    followup, stimulus, episode, expected_goal, prior_memory,
                    tools, notification_interaction,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.warning(
                "[ACQUISITION] episode=E%s goal=G%s continuation=post_acquisition_1 "
                "backend=%s request=failed",
                episode.id, expected_goal.id, backend.identifier,
            )
            if job_work:
                raise
            return False, (None if action is None else InitiativeEffectOutcome(
                action, status or "rejected", result_text or '{"status": "rejected"}'
            ))
        LOGGER.info(
            "[ACQUISITION] episode=E%s goal=G%s continuation=post_acquisition_1 "
            "backend=%s request=completed",
            episode.id, expected_goal.id, backend.identifier,
        )
        if second_acquisition is not None and self._active_goal is expected_goal:
            final_completed, final_effect = await self._request_final_effect_decision(
                stimulus, episode, expected_goal, prior_memory, acquisitions,
                notification_interaction,
                job_work=job_work,
            )
            return final_completed, final_effect
        return True, (None if action is None else InitiativeEffectOutcome(
            action, status or "rejected", result_text or '{"status": "rejected"}'
        ))

    async def _request_final_effect_decision(
        self, stimulus: AttentionStimulus, episode: AttentionEpisode,
        expected_goal: ActiveGoal, prior_memory,
        acquisitions: list[InitiativeAcquisitionOutcome],
        notification_interaction: InteractionContext | None = None,
        *, job_work: bool = False,
    ) -> tuple[bool, InitiativeEffectOutcome | None]:
        backend = self._cognition_backend
        assert backend is not None
        followup = AcquisitionFollowupStimulus(tuple(acquisitions))
        tools = self._effect_tools_for_episode(job_work=job_work)
        action = status = result_text = None
        consumed = False
        LOGGER.info(
            "[ACQUISITION] episode=E%s goal=G%s continuation=post_acquisition_2 "
            "backend=%s request=started",
            episode.id, expected_goal.id, backend.identifier,
        )

        async def execute_tool(call: CognitionToolCall) -> CognitionToolResult:
            nonlocal action, status, result_text, consumed
            if consumed:
                return self._rejected_tool(call.name, "final effect decision already consumed",
                                           log_prefix="ACQUISITION")
            consumed = True
            available = self._effect_tools_for_episode(job_work=job_work)
            LOGGER.info(
                "[ACQUISITION] episode=E%s goal=G%s tool=%s class=effect "
                "status=requested", episode.id, expected_goal.id, call.name,
            )
            if (self.state is not LifecycleState.RUNNING
                    or self._active_goal is not expected_goal
                    or not any(tool.name == call.name for tool in available)):
                result = self._rejected_tool(call.name, "effect capability is not available",
                                             log_prefix="ACQUISITION")
            else:
                action = call.name
                result = await self._execute_initiative_tool(
                    call, available=available, log_prefix="ACQUISITION",
                    notification_interaction=notification_interaction,
                )
            result_text = result.output
            try:
                status = json.loads(result.output).get("status", "rejected")
            except (json.JSONDecodeError, AttributeError):
                status = "rejected"
            if action is not None:
                self.attention.record_action(action, status)
            LOGGER.info(
                "[ACQUISITION] episode=E%s goal=G%s tool=%s class=effect status=%s",
                episode.id, expected_goal.id, call.name, status,
            )
            return result

        try:
            await backend.respond(
                ACQUISITION_FOLLOWUP_REQUEST,
                instructions=self._acquisition_followup_instructions(
                    followup, stimulus, episode, expected_goal, prior_memory,
                    tools, notification_interaction,
                ), tools=tools, tool_executor=execute_tool if tools else None,
                refreshed_instructions=(lambda: self._acquisition_followup_instructions(
                    followup, stimulus, episode, expected_goal, prior_memory,
                    tools, notification_interaction,
                )) if tools else None,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.warning(
                "[ACQUISITION] episode=E%s goal=G%s continuation=post_acquisition_2 "
                "backend=%s request=failed",
                episode.id, expected_goal.id, backend.identifier,
            )
            if job_work:
                raise
            return False, (None if action is None else InitiativeEffectOutcome(
                action, status or "rejected", result_text or '{"status": "rejected"}'
            ))
        LOGGER.info(
            "[ACQUISITION] episode=E%s goal=G%s continuation=post_acquisition_2 "
            "backend=%s request=completed",
            episode.id, expected_goal.id, backend.identifier,
        )
        return True, (None if action is None else InitiativeEffectOutcome(
            action, status or "rejected", result_text or '{"status": "rejected"}'
        ))

    def _continuation_instructions(
        self, continuation: InitiativeContinuationStimulus,
        stimulus: AttentionStimulus, episode: AttentionEpisode,
        expected_goal: ActiveGoal, working_memory,
        tools: tuple[CognitionToolDefinition, ...] = (),
        notification_interaction: InteractionContext | None = None,
    ) -> str:
        return "\n\n".join((
            compose_cognition_instructions(
                self.cognition_context(), self.temporal_context(), self.temporal_situation(),
                self.options.startup_prompt, working_memory,
                expected_goal if self._active_goal is expected_goal else None,
            ),
            *self._notification_sections(tools, notification_interaction),
            episode.render(), stimulus.render(actions_enabled=None),
            continuation.render(),
        ))

    async def _request_continuation(
        self, stimulus: AttentionStimulus, episode: AttentionEpisode,
        expected_goal: ActiveGoal, prior_memory,
        first_effect: InitiativeEffectOutcome,
        acquisitions: tuple[InitiativeAcquisitionOutcome, ...] = (),
        notification_interaction: InteractionContext | None = None,
        *, job_work: bool = False,
    ) -> tuple[bool, InitiativeEffectOutcome | None]:
        backend = self._cognition_backend
        assert backend is not None
        tools = self._continuation_tools_for_episode(
            first_effect.name, job_work=job_work
        )
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
            LOGGER.info(
                "[CONTINUATION] episode=E%s goal=G%s tool=%s class=effect "
                "status=requested",
                episode.id, expected_goal.id, call.name,
            )
            available = self._continuation_tools_for_episode(
                first_effect.name, job_work=job_work
            )
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
                    call, available=available, log_prefix="CONTINUATION",
                    notification_interaction=notification_interaction,
                )
            result_text = result.output
            try:
                status = json.loads(result.output).get("status", "rejected")
            except (json.JSONDecodeError, AttributeError):
                status = "rejected"
            self.attention.record_continuation(action_status=status)
            LOGGER.info(
                "[CONTINUATION] episode=E%s goal=G%s tool=%s class=effect status=%s",
                episode.id, expected_goal.id, call.name, status,
            )
            return result

        LOGGER.info(
            "[CONTINUATION] episode=E%s goal=G%s backend=%s request=started "
            "capabilities=enabled",
            episode.id, expected_goal.id, backend.identifier,
        )
        try:
            response = await backend.respond(
                CONTINUATION_INITIATIVE_REQUEST,
                instructions=self._continuation_instructions(
                    continuation, stimulus, episode, expected_goal, prior_memory,
                    tools, notification_interaction,
                ),
                tools=tools,
                tool_executor=execute_tool,
                refreshed_instructions=lambda: self._continuation_instructions(
                    continuation, stimulus, episode, expected_goal, prior_memory,
                    tools, notification_interaction,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.attention.record_continuation(state="failed")
            LOGGER.warning(
                "[CONTINUATION] episode=E%s goal=G%s backend=%s request=failed",
                episode.id, expected_goal.id, backend.identifier,
            )
            if job_work:
                raise
            return False, (
                InitiativeEffectOutcome(action, status or "rejected", result_text or "")
                if action is not None else None
            )
        self.attention.record_continuation(state="completed", response=response)
        LOGGER.info(
            "[CONTINUATION] episode=E%s goal=G%s backend=%s request=completed "
            "response_chars=%s",
            episode.id, expected_goal.id, backend.identifier, len(response),
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
            "[OUTCOME] episode=E%s goal=G%s backend=%s request=started closure=%s",
            episode.id, expected_goal.id, backend.identifier,
            "enabled" if tools else "disabled",
        )

        async def execute_tool(call: CognitionToolCall) -> CognitionToolResult:
            LOGGER.info(
                "[OUTCOME] episode=E%s goal=G%s tool=%s class=effect status=requested",
                episode.id, expected_goal.id, call.name,
            )
            result = self._execute_outcome_tool(
                call, expected_goal, all_applied
            )
            LOGGER.info(
                "[OUTCOME] episode=E%s goal=G%s tool=%s class=effect status=%s",
                episode.id, expected_goal.id, call.name,
                self._tool_result_status(result),
            )
            return result

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
            LOGGER.warning(
                "[OUTCOME] episode=E%s goal=G%s backend=%s request=failed",
                episode.id, expected_goal.id, backend.identifier,
            )
            return
        self.attention.record_outcome(state="completed", response=response)
        LOGGER.info(
            "[OUTCOME] episode=E%s goal=G%s backend=%s request=completed "
            "response_chars=%s",
            episode.id, expected_goal.id, backend.identifier, len(response),
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
            and (
                self._current_task_binding is None
                or self._current_task_binding.active_goal is not expected_goal
            )
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

    def _initiative_tools_for_episode(
        self, *, job_work: bool
    ) -> tuple[CognitionToolDefinition, ...]:
        if not job_work:
            return self.initiative_tools()
        return (*self.acquisition_tools(), *self._effect_tools_for_episode(
            job_work=True
        ))

    def _effect_tools_for_episode(
        self, *, job_work: bool
    ) -> tuple[CognitionToolDefinition, ...]:
        tools = self.effect_tools()
        if not job_work:
            return tools
        return tuple(tool for tool in tools if tool.name != SCHEDULE_FOLLOWUP_TOOL.name)

    def _continuation_tools_for_episode(
        self, first_effect_name: str, *, job_work: bool
    ) -> tuple[CognitionToolDefinition, ...]:
        if not job_work:
            return self.continuation_tools(first_effect_name)
        if not self.options.initiative_continuation_enabled:
            return ()
        return tuple(
            tool for tool in self._effect_tools_for_episode(job_work=True)
            if tool.name != first_effect_name
        )

    def acquisition_tools(self) -> tuple[CognitionToolDefinition, ...]:
        """Project only read-only autonomous acquisition capabilities."""
        if not (self.options.initiative_enabled and
                self.state is LifecycleState.RUNNING and self._active_goal is not None):
            return ()
        visual = (OBSERVE_SCENE_TOOL,) if self.visual_perception_available() else ()
        recall = (RECALL_MEMORY_TOOL,) if self._memory_recall is not None else ()
        history = ((INSPECT_RUN_HISTORY_TOOL,)
                   if self._run_history_evidence is not None else ())
        return (INSPECT_SELF_TOOL, *visual, *recall, *history)

    @staticmethod
    def _acquisition_tool_names() -> tuple[str, ...]:
        return (INSPECT_SELF_TOOL.name, OBSERVE_SCENE_TOOL.name,
                RECALL_MEMORY_TOOL.name, INSPECT_RUN_HISTORY_TOOL.name)

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
        sink = self._operator_message_sink
        if (self.options.initiative_messages_enabled and sink is not None and
                resolve_notification_route(sink.channel) is not None):
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
        if self._current_task_binding is None:
            tools.append(
                SET_GOAL_TOOL if self._active_goal is None else RESOLVE_GOAL_TOOL
            )
        if (
            self.state is LifecycleState.RUNNING
            and self.options.initiative_enabled
            and self._active_goal is not None
            and self.temporal.pending is None
        ):
            tools.append(SCHEDULE_FOLLOWUP_TOOL)
        tools.append(INSPECT_SELF_TOOL)
        if self.visual_perception_available():
            tools.append(OBSERVE_SCENE_TOOL)
        if self._memory_recall is not None:
            tools.append(RECALL_MEMORY_TOOL)
            tools.append(REMEMBER_TOOL)
        if self._run_history_evidence is not None:
            tools.append(INSPECT_RUN_HISTORY_TOOL)
        return tuple(tools)

    def _execute_memory_admission(
        self, call: CognitionToolCall, current_utterance: str, source_label: str,
        *, episode_id: int | None = None,
    ) -> CognitionToolResult:
        episode = "none" if episode_id is None else f"E{episode_id}"
        LOGGER.info("[MEMORY] episode=%s admission status=requested", episode)
        try:
            if self.state is not LifecycleState.RUNNING or self._memory_admission is None:
                raise RuntimeError("persistent memory admission is not available")
            required = {
                "subject", "kind", "predicate", "value", "evidence",
                "related_entity", "related_role",
            }
            arguments = json.loads(call.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
            if set(arguments) != required:
                raise ValueError(
                    "exactly subject, kind, predicate, value, evidence, "
                    "related_entity, and related_role are required"
                )
            proposal = MemoryAdmissionProposal(**arguments)
            result = self._memory_admission.admit(
                proposal, current_utterance=current_utterance,
                source_label=source_label,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            LOGGER.info(
                "[MEMORY] admission result=rejected reason=validation episode=%s "
                "status=rejected", episode,
            )
            return CognitionToolResult(json.dumps({
                "status": "rejected", "reason": "invalid_tool_arguments",
                "error": str(error),
            }, sort_keys=True))
        except Exception:
            LOGGER.info(
                "[MEMORY] admission result=rejected reason=backend episode=%s "
                "status=rejected", episode
            )
            return CognitionToolResult(json.dumps({
                "status": "rejected", "reason": "backend_failure",
                "error": "persistent memory backend failure",
            }, sort_keys=True))
        if result.status == "applied":
            LOGGER.info(
                "[MEMORY] admission result=%s episode=%s status=applied "
                "entity=%s memory=%s",
                result.admission, episode, result.entity, result.memory,
            )
        else:
            LOGGER.info(
                "[MEMORY] admission result=rejected reason=%s episode=%s "
                "status=rejected", result.reason, episode,
            )
        return CognitionToolResult(json.dumps(result.as_dict(), sort_keys=True))

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
        if call.name == ORIENT_BODY_TOOL.name:
            return await self._execute_orient_body(
                call, available=self.cognition_tools(), source="cognition"
            )
        if call.name == SET_GOAL_TOOL.name:
            return self._execute_set_goal(call)
        if call.name == RESOLVE_GOAL_TOOL.name:
            return self._execute_resolve_goal(call, expected_goal=expected_goal)
        if call.name == SCHEDULE_FOLLOWUP_TOOL.name:
            return self._execute_schedule_followup(
                call, available=self.cognition_tools(), log_prefix="COGNITION",
                expected_goal=expected_goal,
            )
        if call.name == INSPECT_SELF_TOOL.name:
            result, _ = self._execute_self_inspection(call)
            return result
        if call.name == OBSERVE_SCENE_TOOL.name:
            result, _ = await self._execute_visual_perception(call)
            return result
        if call.name == RECALL_MEMORY_TOOL.name:
            return self._execute_memory_recall(call)
        if call.name == INSPECT_RUN_HISTORY_TOOL.name:
            return self._execute_run_history_inspection(call)
        return self._rejected_tool(call.name, "tool is not available")

    def _execute_run_history_inspection(
        self, call: CognitionToolCall, *, expected_goal: ActiveGoal | None = None,
        autonomous: bool = False, episode_id: int | None = None,
    ) -> CognitionToolResult:
        operation = "invalid"
        selector = "none"
        try:
            if self.state is not LifecycleState.RUNNING or self._run_history_evidence is None:
                raise RuntimeError("run history inspection is not available")
            if autonomous and (expected_goal is None or self._active_goal is not expected_goal):
                raise RuntimeError("expected active goal is no longer current")
            operation, selector, query = self._normalize_run_history_arguments(call)
            run = None if operation == "recent" else selector
            result = self._run_history_evidence.inspect(operation, run, query)
        except (json.JSONDecodeError, TypeError, ValueError, RuntimeError):
            result = {"status": "rejected", "reason": "invalid_tool_arguments"}
        status = str(result.get("status", "rejected"))
        reason = result.get("reason")
        count = len(result.get("matches", [])) if isinstance(result.get("matches"), list) else 0
        LOGGER.info(
            "[HISTORY] episode=%s operation=%s run=%s status=%s matches=%s reason=%s",
            f"E{episode_id}" if episode_id is not None else "none", operation,
            selector, status, count, reason or "none",
        )
        return CognitionToolResult(json.dumps(result, ensure_ascii=False, sort_keys=True))

    @staticmethod
    def _normalize_run_history_arguments(
        call: CognitionToolCall,
    ) -> tuple[str, str, str | None]:
        arguments = json.loads(call.arguments)
        if (not isinstance(arguments, dict)
                or set(arguments) != {"selector", "query"}):
            raise ValueError("invalid arguments")
        raw_selector = arguments["selector"]
        query = arguments["query"]
        if type(raw_selector) is not str or (query is not None and type(query) is not str):
            raise ValueError("invalid arguments")
        selector = (
            raw_selector if raw_selector in ("recent", "current", "previous", "previous_day")
            else canonical_run_id(raw_selector)
        )
        if selector is None:
            raise ValueError("invalid selector")
        if query is not None and (not query.strip() or len(query) > MAX_GREP_QUERY_LENGTH):
            raise ValueError("invalid query")
        if selector == "recent":
            if query is not None:
                raise ValueError("recent does not support search")
            return "recent", selector, None
        return ("overview" if query is None else "search"), selector, query

    def _execute_memory_recall(
        self, call: CognitionToolCall, *, expected_goal: ActiveGoal | None = None,
        autonomous: bool = False,
    ) -> CognitionToolResult:
        try:
            if self.state is not LifecycleState.RUNNING or self._memory_recall is None:
                raise RuntimeError("persistent memory recall is not available")
            if autonomous and (expected_goal is None or self._active_goal is not expected_goal):
                raise RuntimeError("expected active goal is no longer current")
            arguments = self._tool_arguments(call, {"query"})
            result = self._memory_recall.recall(arguments["query"])
        except Exception as error:
            return self._rejected_tool(call.name, str(error))
        rendered = result.render()
        memory_count = sum(len(entity.memories) for entity in result.entities)
        LOGGER.info(
            "[MEMORY] recall result=%s entities=%s memories=%s truncated=%s",
            result.result, result.matched_entities, memory_count,
            str(result.truncated).lower(),
        )
        return CognitionToolResult(json.dumps({
            "status": "applied", "recall": rendered,
        }, ensure_ascii=False, sort_keys=True))

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
            owner = self._visual_perception_resource_owner(
                expected_goal, autonomous=autonomous
            )
            frame = self._capture_camera_frame_for_owner(owner)
            observed_at = self._aware_wall_clock()
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
            result = VisualPerceptionResult(
                focus, description[:2000], truncated, observed_at
            )
        except ResourceBusyError:
            self.observability.increment("vision_failures")
            self.observability.event("perception", "vision_acquisition", "rejected")
            error = RuntimeError("camera resource is busy")
            LOGGER.info("[PERCEPTION] modality=visual status=rejected")
            if autonomous:
                self.attention.record_visual(
                    state="failed", focus=focus or None, status="rejected"
                )
            return CognitionToolResult(json.dumps({
                "status": "rejected", "error": str(error),
            }, sort_keys=True)), None
        except Exception as error:
            self.observability.increment("vision_failures")
            self.observability.event("perception", "vision_acquisition", "failed",
                                     severity="error", error=type(error).__name__)
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
        self.observability.increment("vision_acquisitions")
        self.observability.event("perception", "vision_acquisition", "completed",
                                 source=backend.identifier)
        if autonomous:
            self.attention.record_visual(state="completed", focus=focus, status="applied")
        return CognitionToolResult(json.dumps({
            "status": "applied", "focus": result.focus,
            "description": result.description, "truncated": result.truncated,
            "observed_at": result.observed_at.isoformat(timespec="seconds"),
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
            result = replace(
                self._inspect_area(area), observed_at=self._aware_wall_clock()
            )
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
            "observed_at": result.observed_at.isoformat(timespec="seconds"),
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
        notification_interaction: InteractionContext | None = None,
    ) -> CognitionToolResult:
        projected = self.initiative_tools() if available is None else available
        if call.name == ORIENT_BODY_TOOL.name:
            return await self._execute_orient_body(
                call, available=projected, source="initiative",
                log_prefix=log_prefix,
            )
        if call.name == ADDRESS_OPERATOR_TOOL.name:
            return await self._execute_address_operator(
                call, available=projected, log_prefix=log_prefix,
                notification_interaction=notification_interaction,
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
        expected_goal: ActiveGoal | None = None,
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
            if expected_goal is not None and goal is not expected_goal:
                raise RuntimeError("expected active goal is no longer current")
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
        notification_interaction: InteractionContext | None = None,
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
            current_route = resolve_notification_route(sink.channel)
            if current_route is None:
                raise RuntimeError("operator message channel is not an eligible notification route")
            interaction = notification_interaction or current_route
            if interaction != current_route:
                raise RuntimeError("operator message channel changed during cognition")
            await sink.deliver(OperatorMessage(message, "initiative", interaction))
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

    async def _execute_deliver_message(
        self, call: CognitionToolCall,
        available: tuple[CognitionToolDefinition, ...],
        authorized: Sequence[OperatorDeliveryDestination],
        source: str, *, episode_id: int | None = None,
    ) -> CognitionToolResult:
        """Apply one operator-authorized delivery through the current route."""
        try:
            tool = next((item for item in available if item.name == call.name), None)
            if tool is None:
                raise RuntimeError("tool is not available")
            arguments = self._tool_arguments(call, {"destination", "message"})
            destination = arguments["destination"]
            value = arguments["message"]
            if type(destination) is not str:
                raise ValueError("destination must be a string")
            LOGGER.info(
                "[INTERACTION] episode=%s mode=delivery destination=%s "
                "source=%s status=requested",
                "none" if episode_id is None else f"E{episode_id}",
                destination, source,
            )
            projected = {item.name: item for item in authorized}
            if destination not in projected:
                raise ValueError("destination is not authorized for this operator stage")
            if type(value) is not str:
                raise ValueError("message must be a string")
            message = value.strip()
            if not message:
                raise ValueError("message must be non-empty")
            if len(message) > MAX_OPERATOR_MESSAGE_CHARS:
                raise ValueError(
                    f"message must be at most {MAX_OPERATOR_MESSAGE_CHARS} characters"
                )
            if any(
                unicodedata.category(character) == "Cc" and character not in "\n\t"
                for character in message
            ):
                raise ValueError("message contains unsupported control characters")
            if self.state is not LifecycleState.RUNNING:
                raise RuntimeError("operator delivery requires a running application")
            route = self._operator_delivery_routes.resolve(destination)
            if route is None:
                raise RuntimeError("delivery destination is no longer available")
            captured = projected[destination]
            if (route.destination.channel != captured.channel or
                    route.sink.channel != route.destination.channel):
                raise RuntimeError("delivery destination is no longer compatible")
            interaction = operator_delivery(route.destination.channel)
            await route.sink.deliver(OperatorMessage(message, source, interaction))
        except Exception as error:
            LOGGER.info(
                "[INTERACTION] episode=%s mode=delivery destination=%s "
                "source=%s status=rejected",
                "none" if episode_id is None else f"E{episode_id}",
                locals().get("destination", "unknown"), source,
            )
            return self._rejected_tool(call.name, str(error))
        LOGGER.info(
            "[INTERACTION] episode=%s mode=delivery destination=%s source=%s "
            "chars=%s status=applied",
            "none" if episode_id is None else f"E{episode_id}",
            destination, source, len(message),
        )
        return CognitionToolResult(json.dumps({
            "status": "applied", "destination": destination,
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
    def _tool_result_status(result: CognitionToolResult) -> str:
        """Project a tool result to the stable applied/rejected log vocabulary."""
        try:
            status = json.loads(result.output).get("status")
        except (json.JSONDecodeError, AttributeError):
            return "rejected"
        return "rejected" if status == "rejected" else "applied"

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
            battery_observed_at=state.power.observed_at,
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
            except asyncio.CancelledError as error:
                # Cancellation is cleanup control flow, not a reflex failure.
                failure = failure or error
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
