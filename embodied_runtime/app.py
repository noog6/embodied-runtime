"""Application lifecycle orchestration."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace
import json
import logging
import math

from embodied_runtime.body.base import BodyBackend
from embodied_runtime.attention import (
    ACTION_INITIATIVE_REQUEST, INITIATIVE_REQUEST, AttentionStimulus,
    GoalAttentionController, InitiativeOutcome,
)
from embodied_runtime.cognition import (
    ActiveGoal,
    CognitionContext,
    CognitionToolCall,
    CognitionToolDefinition,
    CognitionToolResult,
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
from embodied_runtime.profile import RobotProfile
from embodied_runtime.reflexes import Reflex
from embodied_runtime.sensing.camera import CameraBackend, CameraFrame
from embodied_runtime.platform import (
    HostPlatformProvider,
    PlatformMonitor,
    PlatformMonitorPolicy,
    PlatformProvider,
    PlatformSnapshot,
)
from embodied_runtime.state import BodyState, LifecycleState, PresenceState, RuntimeState

LOGGER = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class ApplicationOptions:
    startup_prompt: str | None = None
    initiative_enabled: bool = False
    initiative_actions_enabled: bool = False


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
    ) -> None:
        self.profile = profile
        self.hardware = hardware
        self.options = options or ApplicationOptions()
        self.events = events or EventBus()
        self.body_backend = body_backend
        self.camera_backend = camera_backend
        self._cognition_backend = cognition_backend
        self.working_memory = (
            working_memory if working_memory is not None else WorkingMemory()
        )
        self._active_goal: ActiveGoal | None = None
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
        self.attention = GoalAttentionController(
            enabled=self.options.initiative_enabled,
            backend_available=self._cognition_backend is not None,
            is_running=lambda: self.state is LifecycleState.RUNNING,
            has_active_goal=lambda: self._active_goal is not None,
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

    def set_goal(self, description: object) -> ActiveGoal:
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Setting a goal requires a running application")
        normalized = validate_goal_description(description)
        if self._active_goal is not None:
            raise RuntimeError("an active goal already exists")
        goal = ActiveGoal(normalized)
        self._active_goal = goal
        LOGGER.info("[GOAL] status=active chars=%s", len(normalized))
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
        LOGGER.info("[GOAL] status=%s", outcome)
        return previous

    def clear_goal(self) -> bool:
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Clearing a goal requires a running application")
        cleared = self._active_goal is not None
        self._active_goal = None
        if cleared:
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

    async def request_cognition(self, message: str) -> str:
        """Request one independent text response through the application boundary."""
        if self.state is not LifecycleState.RUNNING:
            raise RuntimeError("Cognition requires a running application")
        if self._cognition_backend is None:
            raise RuntimeError("No cognition backend is configured")
        if not message or not message.strip():
            raise ValueError("Cognition message must be non-empty")
        backend = self._cognition_backend
        prior_memory = self.working_memory.snapshot()
        instructions = self._cognition_instructions(prior_memory)
        tools = self.cognition_tools()
        tool_outcomes: list[WorkingMemoryToolOutcome] = []

        async def execute_tool(call: CognitionToolCall) -> CognitionToolResult:
            result = await self._execute_cognition_tool(call)
            tool_outcomes.append(WorkingMemoryToolOutcome(call.name, result.output))
            return result
        LOGGER.info("[COGNITION] backend=%s request=started", backend.identifier)
        try:
            response = await backend.respond(
                message,
                instructions=instructions,
                tools=tools,
                tool_executor=execute_tool if tools else None,
                refreshed_instructions=(
                    lambda: self._cognition_instructions(prior_memory)
                ) if tools else None,
            )
        except Exception:
            LOGGER.warning("[COGNITION] backend=%s request=failed", backend.identifier)
            raise
        LOGGER.info(
            "[COGNITION] backend=%s request=completed response_chars=%s",
            backend.identifier,
            len(response),
        )
        self.working_memory.append(message, response, tool_outcomes)
        return response

    def _cognition_instructions(self, working_memory=None) -> str:
        if working_memory is None:
            working_memory = self.working_memory.snapshot()
        return compose_cognition_instructions(
            self.cognition_context(), self.options.startup_prompt, working_memory,
            self._active_goal,
        )

    def _attention_instructions(
        self, stimulus: AttentionStimulus, working_memory, *, actions_enabled: bool
    ) -> str:
        return (
            f"{self._cognition_instructions(working_memory)}\n\n"
            f"{stimulus.render(actions_enabled=actions_enabled)}"
        )

    async def _request_initiative(self, stimulus: AttentionStimulus) -> InitiativeOutcome:
        backend = self._cognition_backend
        if backend is None:
            raise RuntimeError("No cognition backend is configured")
        actions_enabled = self.options.initiative_actions_enabled
        prior_memory = self.working_memory.snapshot()
        instructions = self._attention_instructions(
            stimulus, prior_memory, actions_enabled=actions_enabled
        )
        tools = self.initiative_tools()
        action: str | None = None
        action_status: str | None = None

        async def execute_tool(call: CognitionToolCall) -> CognitionToolResult:
            nonlocal action, action_status
            action = call.name
            LOGGER.info("[INITIATIVE] tool=%s status=requested", call.name)
            result = await self._execute_initiative_tool(call)
            try:
                action_status = json.loads(result.output).get("status", "rejected")
            except (json.JSONDecodeError, AttributeError):
                action_status = "rejected"
            self.attention.record_action(action, action_status)
            return result

        LOGGER.info(
            "[INITIATIVE] backend=%s request=started actions=%s",
            backend.identifier, "enabled" if actions_enabled else "disabled",
        )
        try:
            response = await backend.respond(
                ACTION_INITIATIVE_REQUEST if actions_enabled else INITIATIVE_REQUEST,
                instructions=instructions,
                tools=tools,
                tool_executor=execute_tool if tools else None,
                refreshed_instructions=(
                    lambda: self._attention_instructions(
                        stimulus, prior_memory, actions_enabled=True
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
        return InitiativeOutcome(response, action, action_status)

    def initiative_tools(self) -> tuple[CognitionToolDefinition, ...]:
        """Project the sole bounded autonomous capability at request time."""
        body = self.body_backend
        if (
            self.options.initiative_enabled
            and self.options.initiative_actions_enabled
            and self.state is LifecycleState.RUNNING
            and self._active_goal is not None
            and body is not None
            and not body.is_physical
            and "orientation" in body.capabilities
        ):
            return (ORIENT_BODY_TOOL,)
        return ()

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
        return tuple(tools)

    async def _execute_cognition_tool(
        self, call: CognitionToolCall
    ) -> CognitionToolResult:
        LOGGER.info("[COGNITION] tool=%s status=requested", call.name)
        if call.name == ORIENT_BODY_TOOL.name:
            return await self._execute_orient_body(
                call, available=self.cognition_tools(), source="cognition"
            )
        if call.name == SET_GOAL_TOOL.name:
            return self._execute_set_goal(call)
        if call.name == RESOLVE_GOAL_TOOL.name:
            return self._execute_resolve_goal(call)
        return self._rejected_tool(call.name, "tool is not available")

    async def _execute_initiative_tool(
        self, call: CognitionToolCall
    ) -> CognitionToolResult:
        if call.name != ORIENT_BODY_TOOL.name:
            return self._rejected_tool(
                call.name, "tool is not available", log_prefix="INITIATIVE"
            )
        return await self._execute_orient_body(
            call, available=self.initiative_tools(), source="initiative",
            log_prefix="INITIATIVE",
        )

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

    def _execute_resolve_goal(self, call: CognitionToolCall) -> CognitionToolResult:
        try:
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
        state = self._runtime_state
        platform = state.platform
        body = self.body_summary()
        camera = self.camera_summary()
        return CognitionContext(
            profile_id=self.profile.identifier,
            profile_name=self.profile.name,
            profile_description=self.profile.description,
            lifecycle=state.lifecycle.value,
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
