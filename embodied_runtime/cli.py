"""Command-line interface for the runtime."""

import argparse
import asyncio
from collections.abc import Sequence
from contextlib import ExitStack
import logging
import math
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, Coroutine

from embodied_runtime.app import (
    ApplicationOptions, LifecycleState, RobotApplication, RuntimeSummary,
)
from embodied_runtime.earcons import EarconPlayer, FusionHatEarconOutput
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cognition import TextCognitionBackend
from embodied_runtime.cognition.openai_responses import OpenAIResponsesBackend
from embodied_runtime.console import AsyncLineTerminal, RuntimeConsole, run_console_session
from embodied_runtime.config import (
    ConfigurationError, LaunchConfiguration, load_runtime_config,
    resolve_launch_configuration,
)
from embodied_runtime.hardware.base import HardwareBackend
from embodied_runtime.hardware.fusion_hat import (
    FusionHatHardwareBackend,
    FusionHatUnavailableError,
    SERVO_CENTER_PULSE_US,
    SERVO_PERIOD_US,
    normalize_pwm_channel,
)
from embodied_runtime.hardware.host import HostHardwareBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.logging_config import configure_logging
from embodied_runtime.run_history import (
    DEFAULT_HISTORY_ROOT, RunHistory, RunHistoryEvidenceReader, RunHistorySetupError,
    start_run,
)
from embodied_runtime.sms import TwilioSmsService, TwilioSmsSettings
from embodied_runtime.observability import RunObservability
from embodied_runtime.pricing import BUILT_IN_PRICING
from embodied_runtime.memory import SQLiteMemoryStore
from embodied_runtime.jobs import (
    FilesystemJobWorkspaceStore, SQLiteJobStore, workspace_root_for_database,
)
from embodied_runtime.interaction import (
    ConsoleOperatorMessageChannel, InteractionChannel, InteractionEnvironment,
    OperatorDeliveryDestination, OperatorDeliveryRoute,
    OperatorDeliveryRouteCatalog,
)
from embodied_runtime.profile import ProfileLoadError, RobotProfile, load_profile
from embodied_runtime.resources import ResourceArbiter
from embodied_runtime.reflexes import PresenceCenteringReflex
from embodied_runtime.platform import PlatformMonitorPolicy, PlatformSnapshot
from embodied_runtime.power import PowerMonitorPolicy
from embodied_runtime.perception import (
    OpenAIResponsesVisualPerceptionBackend, VisualPerceptionBackend,
)
from embodied_runtime.sensing.camera import CameraBackend
from embodied_runtime.sensing.camera.picamera2 import (
    Picamera2CameraBackend,
    Picamera2UnavailableError,
)
from embodied_runtime.voice import (
    FusionHatEspeakTTSProvider,
    FusionHatElevenLabsTTSProvider,
    FusionHatOpenAITTSProvider,
    FusionHatPiperTTSProvider,
    FusionHatVoiceProvider,
    FallbackTextToSpeechProvider,
    OpenAITTSUnavailableError,
    ElevenLabsTTSUnavailableError,
    PiperTTSUnavailableError,
    VoiceSessionPolicy,
)

LOGGER = logging.getLogger(__name__)


def _live_non_daemon_thread_names(
    threads: Sequence[threading.Thread],
    *,
    current: threading.Thread,
    main_thread: threading.Thread,
) -> tuple[str, ...]:
    """Return bounded process-exit-relevant thread metadata."""
    return tuple(sorted(
        thread.name for thread in threads
        if thread is not current and thread is not main_thread
        and thread.is_alive() and not thread.daemon
    ))


def _run_with_asyncio_cleanup(coroutine: Coroutine[Any, Any, int]) -> int:
    """Run the application while exposing asyncio's otherwise hidden cleanup tail."""
    runner = asyncio.Runner()
    try:
        result = runner.run(coroutine)
        LOGGER.info("[PROCESS] application_coroutine status=completed")
        return result
    finally:
        LOGGER.info("[PROCESS] asyncio_cleanup status=started")
        started = time.perf_counter()
        runner.close()
        duration_ms = (time.perf_counter() - started) * 1000
        LOGGER.info(
            "[PROCESS] asyncio_cleanup status=completed duration_ms=%.1f",
            duration_ms,
        )
        names = _live_non_daemon_thread_names(
            threading.enumerate(),
            current=threading.current_thread(),
            main_thread=threading.main_thread(),
        )
        LOGGER.info(
            "[PROCESS] threads non_daemon_alive=%s names=%s",
            len(names),
            ",".join(names) or "none",
        )


def build_parser(*, explicit_configurable_values: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an embodied agent profile")
    parser.add_argument("startup_prompt", nargs="?", help="prompt for a future interaction system")
    def configurable_default(historical: object) -> object:
        return None if explicit_configurable_values else historical

    parser.add_argument("--config", type=Path, help="startup TOML configuration path")
    parser.add_argument("--profile", default=configurable_default("mira"),
                        help="robot profile identifier")
    parser.add_argument(
        "--hardware", choices=("auto", "virtual", "host", "fusion-hat"),
        default=configurable_default("virtual")
    )
    parser.add_argument("--camera", choices=("auto", "none", "picamera2"),
                        default=configurable_default("none"))
    parser.add_argument("--no-color", action="store_true",
                        help="disable ANSI colour in console and runtime logs")
    parser.add_argument(
        "--no-earcons", action="store_true",
        help="disable semantic earcon chimes without disabling voice or TTS",
    )
    parser.add_argument(
        "--cognition", choices=("none", "openai-responses"),
        default=configurable_default("none")
    )
    parser.add_argument(
        "--vision", choices=("none", "openai-responses"),
        default=configurable_default("none"),
    )
    parser.add_argument(
        "--voice", action="store_true", default=configurable_default(False),
        help="enable bounded Fusion HAT voice interaction",
    )
    parser.add_argument(
        "--sms", action="store_true", default=configurable_default(False),
        help="enable the configured inbound Twilio SMS transport",
    )
    parser.add_argument("--tts", choices=("espeak", "piper", "openai", "elevenlabs"),
                        default=configurable_default("espeak"))
    parser.add_argument("--fallback-tts", choices=("none", "espeak"),
                        default=configurable_default("none"))
    parser.add_argument("--piper-model", default=configurable_default(None),
                        help="path to a local Piper .onnx voice model")
    parser.add_argument(
        "--openai-tts-model", default=configurable_default("gpt-4o-mini-tts")
    )
    parser.add_argument(
        "--openai-tts-voice", default=configurable_default("cedar")
    )
    parser.add_argument(
        "--elevenlabs-tts-model", default=configurable_default("eleven_flash_v2_5")
    )
    parser.add_argument("--elevenlabs-tts-voice-id", default=None)
    parser.add_argument(
        "--elevenlabs-tts-speed", type=_elevenlabs_tts_speed,
        default=configurable_default(1.0),
    )
    parser.add_argument("--initiative", action="store_true",
                        default=configurable_default(False),
                        help="enable bounded goal-directed cognition initiative")
    parser.add_argument(
        "--initiative-platform-attention", action="store_true",
        default=configurable_default(False),
        help="also attend to platform condition transitions",
    )
    parser.add_argument(
        "--initiative-actions", action="store_true", default=configurable_default(False),
        help="allow initiative one bounded nonphysical semantic body action",
    )
    parser.add_argument(
        "--initiative-messages", action="store_true", default=configurable_default(False),
        help="allow initiative one bounded message to the operator",
    )
    parser.add_argument(
        "--initiative-continuation", action="store_true",
        default=configurable_default(False),
        help="allow one independent, distinct second initiative effect",
    )
    parser.add_argument(
        "--initiative-goal-closure", action="store_true",
        default=configurable_default(False),
        help="allow one post-effect evaluation to complete the same active goal",
    )
    parser.add_argument(
        "--jobs-max-concurrent-work", type=int, choices=range(1, 257),
        default=configurable_default(1),
        help="maximum concurrent bounded Job cognition tasks (1..256)",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--mode", choices=("run", "console", "diagnostics"),
        default=configurable_default(None),
        help="override the configured runtime mode",
    )
    modes.add_argument("--diagnostics", action="store_true",
                       default=configurable_default(False))
    modes.add_argument("--console", action="store_true",
                       default=configurable_default(False))
    parser.add_argument(
        "--fusion-servo-test",
        metavar="P0..P11",
        type=_pwm_channel,
        help="CAUTION: explicitly actuate one Fusion HAT bench-servo PWM channel",
    )
    parser.add_argument(
        "--fusion-battery-test",
        action="store_true",
        help="read Fusion HAT battery voltage once during diagnostics",
    )
    parser.add_argument(
        "--camera-test",
        metavar="OUTPUT_PATH",
        type=Path,
        help="capture exactly one JPEG to this path during diagnostics",
    )
    return parser


def parse_launch_arguments(
    argv: Sequence[str] | None = None,
) -> tuple[argparse.ArgumentParser, argparse.Namespace, LaunchConfiguration]:
    """Parse CLI presence, load config, and produce one effective launch."""
    parser = build_parser(explicit_configurable_values=True)
    args = parser.parse_args(argv)
    file_config = None
    if args.config is not None:
        try:
            file_config = load_runtime_config(args.config)
        except ConfigurationError as error:
            parser.error(str(error))
    effective = resolve_launch_configuration(args, file_config)
    args.profile = effective.profile
    args.hardware = effective.hardware
    args.camera = effective.camera
    args.cognition = effective.cognition
    args.vision = effective.vision
    args.timezone = effective.timezone
    args.interaction_environment = effective.interaction_environment
    args.console = effective.mode == "console"
    args.diagnostics = effective.mode == "diagnostics"
    args.mode = effective.mode
    args.initiative = effective.initiative
    args.initiative_platform_attention = effective.initiative_platform_attention
    args.initiative_actions = effective.initiative_actions
    args.initiative_messages = effective.initiative_messages
    args.initiative_continuation = effective.initiative_continuation
    args.initiative_goal_closure = effective.initiative_goal_closure
    args.voice_enabled = effective.voice_enabled
    args.voice_wake_word_enabled = effective.voice_wake_word_enabled
    args.voice_wake_words = effective.voice_wake_words
    args.tts = effective.voice_tts
    args.fallback_tts = effective.voice_fallback_tts
    args.piper_model = effective.voice_piper_model
    args.openai_tts_model = effective.voice_openai_tts_model
    args.openai_tts_voice = effective.voice_openai_tts_voice
    args.elevenlabs_tts_model = effective.voice_elevenlabs_tts_model
    args.elevenlabs_tts_voice_id = effective.voice_elevenlabs_tts_voice_id
    args.elevenlabs_tts_speed = effective.voice_elevenlabs_tts_speed
    args.voice_initial_timeout_seconds = effective.voice_initial_timeout_seconds
    args.voice_followup_timeout_seconds = effective.voice_followup_timeout_seconds
    args.earcons_enabled = effective.earcons_enabled
    args.memory_enabled = effective.memory_enabled
    args.memory_database_path = effective.memory_database_path
    args.jobs_enabled = effective.jobs_enabled
    args.jobs_database_path = effective.jobs_database_path
    args.jobs_auto_continue = effective.jobs_auto_continue
    args.jobs_heartbeat_seconds = effective.jobs_heartbeat_seconds
    args.jobs_max_auto_steps = effective.jobs_max_auto_steps
    args.jobs_max_concurrent_work = effective.jobs_max_concurrent_work
    args.jobs_scheduler_poll_seconds = effective.jobs_scheduler_poll_seconds
    args.findings_context_selection_enabled = effective.findings_context_selection_enabled
    args.power_interval_seconds = effective.power_interval_seconds
    args.power_attention_voltage_v = effective.power_attention_voltage_v
    args.power_recovery_voltage_v = effective.power_recovery_voltage_v
    args.sms_enabled = effective.sms_enabled
    args.sms_backend = effective.sms_backend
    args.sms_bind_host = effective.sms_bind_host
    args.sms_bind_port = effective.sms_bind_port
    args.sms_webhook_path = effective.sms_webhook_path
    return parser, args, effective


def _pwm_channel(value: str) -> str:
    try:
        return normalize_pwm_channel(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _elevenlabs_tts_speed(value: str) -> float:
    try:
        speed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "ElevenLabs TTS speed must be a number from 0.7 to 1.2"
        ) from error
    if not math.isfinite(speed) or not 0.7 <= speed <= 1.2:
        raise argparse.ArgumentTypeError(
            "ElevenLabs TTS speed must be from 0.7 to 1.2"
        )
    return speed


def build_hardware_backend(args: argparse.Namespace) -> HardwareBackend:
    if args.hardware == "fusion-hat":
        return FusionHatHardwareBackend()
    if args.hardware == "host":
        return HostHardwareBackend()
    if args.hardware == "virtual":
        return VirtualHardwareBackend()
    fusion = FusionHatHardwareBackend()
    if fusion.sysfs.is_ready:
        resolved: HardwareBackend = fusion
    else:
        resolved = HostHardwareBackend()
    LOGGER.info("[HARDWARE] configured=auto resolved=%s", resolved.identifier)
    return resolved


def build_camera_backend(args: argparse.Namespace) -> CameraBackend | None:
    if args.camera == "picamera2":
        return Picamera2CameraBackend()
    if args.camera == "auto":
        camera = Picamera2CameraBackend()
        try:
            camera.start()
        except Picamera2UnavailableError as error:
            LOGGER.info(
                "[CAMERA] configured=auto resolved=none reason=%s",
                type(error).__name__,
            )
            return None
        LOGGER.info("[CAMERA] configured=auto resolved=%s", camera.identifier)
        return camera
    return None


def _stop_unowned_camera(camera: CameraBackend) -> None:
    """Release a probed camera without replacing the composition failure."""
    try:
        camera.stop()
    except BaseException:
        LOGGER.exception("[CAMERA] pre_application_cleanup_failed")


def build_cognition_backend(args: argparse.Namespace) -> TextCognitionBackend | None:
    if args.cognition == "openai-responses":
        return OpenAIResponsesBackend()
    return None


def build_visual_perception_backend(
    args: argparse.Namespace,
) -> VisualPerceptionBackend | None:
    if args.vision == "openai-responses":
        return OpenAIResponsesVisualPerceptionBackend()
    return None


def build_persistent_memory_store(
    args: argparse.Namespace,
) -> SQLiteMemoryStore | None:
    """Construct the optional durable store at the launch composition boundary."""
    if not args.memory_enabled:
        return None
    args.memory_database_path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteMemoryStore(args.memory_database_path)


def build_job_store(args: argparse.Namespace) -> SQLiteJobStore | None:
    """Construct Jobs independently from the persistent-memory subsystem."""
    if not args.jobs_enabled:
        return None
    args.jobs_database_path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteJobStore(args.jobs_database_path)


def build_job_workspace_store(args: argparse.Namespace) -> FilesystemJobWorkspaceStore | None:
    """Construct the Job-owned Workspace from the configured Jobs database."""
    if not args.jobs_enabled:
        return None
    return FilesystemJobWorkspaceStore(workspace_root_for_database(args.jobs_database_path))


def build_platform_monitor_policy(
    args: argparse.Namespace,
) -> PlatformMonitorPolicy | None:
    """Return the mode-specific monitor policy, preserving headless defaults."""
    if args.console:
        return PlatformMonitorPolicy(heartbeat_interval_seconds=None)
    return None


def build_text_to_speech_provider(
    args: argparse.Namespace, hardware: HardwareBackend | None = None,
    observability: RunObservability | None = None,
):
    """Build the selected physical speech adapter only when voice is available."""
    fusion_selected = (
        isinstance(hardware, FusionHatHardwareBackend)
        if hardware is not None else args.hardware == "fusion-hat"
    )
    if not (args.voice_enabled and fusion_selected):
        return None
    if args.tts == "piper":
        return FusionHatPiperTTSProvider(model_path=args.piper_model)
    if args.tts == "openai":
        return FusionHatOpenAITTSProvider(
            model=args.openai_tts_model, voice=args.openai_tts_voice
        )
    if args.tts == "elevenlabs":
        primary = FusionHatElevenLabsTTSProvider(
            model=args.elevenlabs_tts_model,
            voice_id=args.elevenlabs_tts_voice_id,
            speed=args.elevenlabs_tts_speed,
            observability=observability,
        )
        if getattr(args, "fallback_tts", "none") == "espeak":
            return FallbackTextToSpeechProvider(
                primary, FusionHatEspeakTTSProvider,
                observability=observability,
            )
        return primary
    return FusionHatEspeakTTSProvider()


def run_fusion_servo_test(
    hardware: FusionHatHardwareBackend,
    channel_name: str,
    *,
    sleeper=time.sleep,
) -> str:
    """Perform the sole explicit physical-output diagnostic."""
    channel = hardware.open_pwm_channel(channel_name)
    try:
        channel.disable()
        channel.enable()
        channel.set_period_us(SERVO_PERIOD_US)
        channel.set_pulse_width_us(SERVO_CENTER_PULSE_US)
        sleeper(0.5)
    finally:
        channel.close()
    return (
        f"[FUSION] servo_test channel={channel.name} "
        f"pulse_us={SERVO_CENTER_PULSE_US} period_us={SERVO_PERIOD_US} status=ok"
    )


def run_fusion_battery_test(hardware: FusionHatHardwareBackend) -> str:
    """Read and format one Fusion HAT+ battery-voltage measurement."""
    reading = hardware.read_battery_voltage()
    return (
        f"[BATTERY] voltage_uv={reading.voltage_uv} "
        f"battery_v={reading.battery_voltage:.3f}"
    )


def format_summary(summary: RuntimeSummary) -> str:
    capabilities = ",".join(summary.capabilities) or "none"
    return (
        f"[DIAG] profile={summary.profile_id} name={summary.profile_name!r} "
        f"hardware={summary.hardware_backend} "
        f"physical={str(summary.hardware_is_physical).lower()} "
        f"capabilities={capabilities} "
        f"startup_prompt_provided={str(summary.startup_prompt_provided).lower()} "
        f"lifecycle={summary.lifecycle_status}"
    )


def format_platform(snapshot: PlatformSnapshot) -> str:
    def value(item: object | None) -> str:
        return "unknown" if item is None or item == "" else str(item)

    def decimal(item: float | None, digits: int = 1) -> str:
        return "unknown" if item is None else f"{item:.{digits}f}"

    load_1m = snapshot.load_averages[0] if snapshot.load_averages else None
    mib = 1024 * 1024
    available_mb = (
        round(snapshot.memory_available_bytes / mib)
        if snapshot.memory_available_bytes is not None else None
    )
    total_mb = (
        round(snapshot.memory_total_bytes / mib)
        if snapshot.memory_total_bytes is not None else None
    )
    return (
        f"[PLATFORM] hostname={value(snapshot.hostname)} system={value(snapshot.system)} "
        f"release={value(snapshot.release)} machine={value(snapshot.machine)} "
        f"python={value(snapshot.python_version)} model={value(snapshot.model)!r} "
        f"uptime_s={decimal(snapshot.uptime_seconds)} load_1m={decimal(load_1m, 2)} "
        f"memory_available_mb={value(available_mb)} memory_total_mb={value(total_mb)} "
        f"cpu_temp_c={decimal(snapshot.cpu_temperature_celsius)}"
    )


async def _run_console_application(
    application: RobotApplication, terminal: AsyncLineTerminal,
    message_channel: ConsoleOperatorMessageChannel | None,
    history_root: Path = DEFAULT_HISTORY_ROOT,
) -> int:
    """Own the console's interrupt and cleanup lifecycle in one boundary."""
    try:
        await application.start()
        LOGGER.info("[CONSOLE] mode=local status=ready")
        await run_console_session(
            RuntimeConsole(application, history_root=history_root),
            terminal,
            message_channel,
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        # SIGINT is translated to task cancellation by modern asyncio, while
        # some selector/platform combinations surface KeyboardInterrupt here.
        LOGGER.info("[APP] interrupted")
        raise
    finally:
        await application.stop()
    return 0


async def _run_application(
    args: argparse.Namespace, profile: RobotProfile,
    history_root: Path = DEFAULT_HISTORY_ROOT,
    history_evidence: RunHistoryEvidenceReader | None = None,
    observability: RunObservability | None = None,
    shutdown_requested: asyncio.Event | None = None,
) -> int:
    hardware = build_hardware_backend(args)
    camera = build_camera_backend(args)
    with ExitStack() as composition_cleanup:
        if args.camera == "auto" and camera is not None:
            composition_cleanup.callback(_stop_unowned_camera, camera)
        cognition = build_cognition_backend(args)
        vision = build_visual_perception_backend(args)
        application: RobotApplication | None = None
        sms_service = None
        if args.sms_enabled:
            settings = TwilioSmsSettings.from_environment(
                bind_host=args.sms_bind_host, bind_port=args.sms_bind_port,
                webhook_path=args.sms_webhook_path,
            )

            async def request_sms_cognition(message: str, **kwargs: object) -> str:
                assert application is not None
                return await application.request_cognition(message, **kwargs)

            sms_service = TwilioSmsService(settings, request_sms_cognition)
        message_channel = ConsoleOperatorMessageChannel() if args.console else None
        routes: list[OperatorDeliveryRoute] = []
        if message_channel is not None:
            routes.append(OperatorDeliveryRoute(
                OperatorDeliveryDestination(
                    "console", InteractionChannel.CONSOLE, "local plain-text console"
                ), message_channel,
            ))
        if sms_service is not None:
            routes.append(OperatorDeliveryRoute(
                OperatorDeliveryDestination(
                    "sms", InteractionChannel.REMOTE_TEXT, "configured operator SMS"
                ), sms_service,
            ))
        delivery_routes = OperatorDeliveryRouteCatalog(routes)
        notification_sink = message_channel or sms_service
        persistent_memory = build_persistent_memory_store(args)
        jobs = build_job_store(args)
        try:
            job_workspaces = build_job_workspace_store(args)
        except BaseException:
            if jobs is not None:
                jobs.close()
            if persistent_memory is not None:
                persistent_memory.close()
            raise
        resources = ResourceArbiter()
        application = RobotApplication(
            profile, hardware, ApplicationOptions(startup_prompt=args.startup_prompt,
                                                  initiative_enabled=args.initiative,
                                                  initiative_platform_attention_enabled=args.initiative_platform_attention,
                                                  initiative_actions_enabled=args.initiative_actions,
                                                  initiative_messages_enabled=args.initiative_messages,
                                                  initiative_continuation_enabled=args.initiative_continuation,
                                                  initiative_goal_closure_enabled=args.initiative_goal_closure,
                                                  jobs_auto_continue=args.jobs_auto_continue,
                                                  jobs_heartbeat_seconds=args.jobs_heartbeat_seconds,
                                                  jobs_max_auto_steps=args.jobs_max_auto_steps,
                                                  jobs_max_concurrent_work=args.jobs_max_concurrent_work,
                                                  jobs_scheduler_poll_seconds=args.jobs_scheduler_poll_seconds,
                                                  findings_context_selection_enabled=args.findings_context_selection_enabled,
                                                  voice_enabled=args.voice_enabled,
                                                  voice_wake_word_enabled=args.voice_wake_word_enabled,
                                                  voice_tts_mode=args.tts,
                                                  cognition_backend=args.cognition,
                                                  camera_backend=args.camera,
                                                  diagnostics_enabled=True,
                                                  runtime_mode=("diagnostics" if args.diagnostics
                                                                else "console" if args.console
                                                                else "run")),
            body_backend=(VirtualBodyBackend()
                          if isinstance(hardware, VirtualHardwareBackend) else None),
            reflexes=(PresenceCenteringReflex(),),
            camera_backend=camera,
            cognition_backend=cognition,
            visual_perception_backend=vision,
            operator_message_sink=notification_sink,
            operator_delivery_routes=delivery_routes,
            platform_monitor_policy=build_platform_monitor_policy(args),
            power_monitor_policy=PowerMonitorPolicy(
                interval_seconds=args.power_interval_seconds,
                attention_voltage_v=args.power_attention_voltage_v,
                recovery_voltage_v=args.power_recovery_voltage_v,
            ),
            voice_provider=(FusionHatVoiceProvider()
                            if args.voice_enabled
                            and isinstance(hardware, FusionHatHardwareBackend) else None),
            text_to_speech_provider=build_text_to_speech_provider(
                args, hardware, observability
            ),
            voice_policy=VoiceSessionPolicy(args.voice_initial_timeout_seconds, args.voice_followup_timeout_seconds),
            voice_wake_words=(args.voice_wake_words
                              if args.voice_enabled and args.voice_wake_word_enabled
                              and isinstance(hardware, FusionHatHardwareBackend) else None),
            timezone_name=args.timezone,
            persistent_memory_store=persistent_memory,
            job_store=jobs,
            job_workspace_store=job_workspaces,
            run_history_evidence=history_evidence,
            observability=observability,
            interaction_environment=InteractionEnvironment(args.interaction_environment),
            sms_service=sms_service,
            resource_arbiter=resources,
            earcon_player=EarconPlayer(
                resources,
                FusionHatEarconOutput()
                if args.earcons_enabled
                and isinstance(hardware, FusionHatHardwareBackend) else None,
            ),
        )
        composition_cleanup.pop_all()
    if args.diagnostics:
        try:
            await application.start()
            application.refresh_platform_state()
            print(format_summary(application.summary()))
            assert application.runtime_state.platform is not None
            print(format_platform(application.runtime_state.platform))
            if isinstance(hardware, FusionHatHardwareBackend):
                print(
                    f"[FUSION] driver=ready pwm_channels={len(hardware.pwm_channels)} "
                    "status=ready"
                )
                if args.fusion_servo_test is not None:
                    print(run_fusion_servo_test(hardware, args.fusion_servo_test))
                if args.fusion_battery_test:
                    print(run_fusion_battery_test(hardware))
            if args.camera_test is not None:
                frame = application.capture_camera_frame()
                args.camera_test.write_bytes(frame.data)
                assert camera is not None
                print(
                    f"[CAMERA] backend={camera.identifier} width={frame.width} "
                    f"height={frame.height} media_type={frame.media_type} "
                    f"bytes={len(frame.data)} output={args.camera_test} status=ok"
                )
        finally:
            await application.stop()
        return 0

    if args.console:
        return await _run_console_application(
            application, AsyncLineTerminal(no_color=args.no_color), message_channel,
            history_root,
        )

    return await _run_headless_application(application, shutdown_requested)


async def _run_headless_application(
    application: RobotApplication,
    shutdown_requested: asyncio.Event | None = None,
) -> int:
    """Run one headless application, including the daemon shutdown race."""
    if shutdown_requested is None:
        await application.run()
        return 0
    run_task = asyncio.create_task(application.run(), name="application-run")
    signal_task = asyncio.create_task(shutdown_requested.wait(), name="sigterm-wait")
    done, _ = await asyncio.wait(
        (run_task, signal_task), return_when=asyncio.FIRST_COMPLETED,
    )
    if signal_task in done and not run_task.done():
        application.request_stop()
        if application.state is not LifecycleState.RUNNING:
            # Startup may be blocked in provider preparation. Cancellation is
            # only the wake-up mechanism; RobotApplication.run() still owns
            # partial-start cleanup in its finally block.
            run_task.cancel()
    signal_task.cancel()
    await asyncio.gather(signal_task, return_exceptions=True)
    try:
        await run_task
    except asyncio.CancelledError:
        if not shutdown_requested.is_set():
            raise
    return 0


async def _run_process_application(
    args: argparse.Namespace, profile: RobotProfile, history_root: Path,
    history_evidence: RunHistoryEvidenceReader, observability: RunObservability,
) -> int:
    """Own run-mode process signals while delegating cleanup to the lifecycle."""
    if args.mode != "run":
        return await _run_application(
            args, profile, history_root, history_evidence, observability,
        )
    loop = asyncio.get_running_loop()
    shutdown_requested = asyncio.Event()
    logged = False

    def request_shutdown() -> None:
        nonlocal logged
        if logged:
            return
        logged = True
        LOGGER.info("[PROCESS] signal=SIGTERM action=shutdown_requested")
        shutdown_requested.set()

    installed = False
    try:
        loop.add_signal_handler(signal.SIGTERM, request_shutdown)
        installed = True
    except (NotImplementedError, RuntimeError):
        pass
    try:
        return await _run_application(
            args, profile, history_root, history_evidence, observability,
            shutdown_requested,
        )
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)


def main(
    argv: Sequence[str] | None = None,
    *,
    history_root: Path = DEFAULT_HISTORY_ROOT,
) -> int:
    parser, args, _ = parse_launch_arguments(argv)
    try:
        validate_launch_dependencies(args)
    except ConfigurationError as error:
        parser.error(str(error))
    try:
        profile = load_profile(args.profile)
    except ProfileLoadError as error:
        parser.error(str(error))

    history: RunHistory | None = None
    try:
        history = start_run(
            history_root,
            profile=args.profile,
            hardware=args.hardware,
            config_source=str(args.config) if args.config is not None else None,
        )
    except OSError as error:
        configure_logging(no_color=args.no_color)
        cleanup = (
            "complete" if error.cleanup_complete else "incomplete"
        ) if isinstance(error, RunHistorySetupError) else "not_needed"
        LOGGER.warning(
            "[RUN] history=%s status=unavailable provisional_cleanup=%s",
            history_root,
            cleanup,
        )
    else:
        if not configure_logging(
            no_color=args.no_color, history_log=history.log_path,
        ):
            cleanup_complete = history.abort()
            LOGGER.warning(
                "[RUN] history=%s status=unavailable provisional_cleanup=%s",
                history.directory,
                "complete" if cleanup_complete else "incomplete",
            )
            history = None
        else:
            LOGGER.info(
                "[RUN] id=%s history=%s status=started",
                history.run_id,
                history.directory,
            )
            history.mark_started()
    if args.config is not None:
        LOGGER.info("[CONFIG] source=%s status=loaded", args.config)

    observability = RunObservability(
        history.run_id if history is not None else None,
        pricing=BUILT_IN_PRICING,
    )
    LOGGER.info("[OBS] status=ready")
    try:
        result = _run_with_asyncio_cleanup(
            _run_process_application(
                args, profile, history_root,
                RunHistoryEvidenceReader(history_root, history.run_id,
                                         timezone_name=args.timezone)
                if history is not None else RunHistoryEvidenceReader(
                    history_root, timezone_name=args.timezone), observability,
            )
        )
    except (
        FusionHatUnavailableError, Picamera2UnavailableError,
        PiperTTSUnavailableError,
        OpenAITTSUnavailableError,
        ElevenLabsTTSUnavailableError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        result = 2
    except KeyboardInterrupt:
        result = 130
    if history is not None:
        try:
            status = history.finalize(result)
        except OSError:
            LOGGER.warning(
                "[RUN] id=%s history=%s status=finalization_unavailable",
                history.run_id,
                history.directory,
            )
        else:
            LOGGER.info(
                "[RUN] id=%s status=%s exit_code=%s",
                history.run_id,
                status,
                result,
            )
    status = "completed" if result == 0 else "interrupted" if result == 130 else "failed"
    shutdown = "normal" if result == 0 else "interrupted" if result == 130 else "failed"
    if result == 130:
        observability.increment("interruptions")
    summary = observability.finalize(
        status, shutdown=shutdown,
        directory=history.directory if history is not None else None,
    )
    persistence, persistence_error = observability.summary_persistence
    if persistence == "written" and history is not None:
        LOGGER.info("[OBS] summary_written run=%s path=%s", history.run_id,
                    history.directory / "summary.json")
    elif persistence == "failed" and history is not None:
        LOGGER.warning("[OBS] summary_write_failed run=%s path=%s error=%s",
                       history.run_id, history.directory / "summary.json",
                       persistence_error)
    LOGGER.info("\n%s", observability.banner(summary))
    LOGGER.info("[PROCESS] main status=returning exit_code=%s", result)
    return result


def validate_launch_dependencies(args: argparse.Namespace) -> None:
    """Validate pure cross-field launch rules without constructing runtime adapters."""
    if args.initiative_platform_attention and not args.initiative:
        raise ConfigurationError("--initiative-platform-attention requires --initiative")
    if args.initiative_goal_closure and not args.initiative:
        raise ConfigurationError("--initiative-goal-closure requires --initiative")
    if args.initiative_actions and not args.initiative:
        raise ConfigurationError("--initiative-actions requires --initiative")
    if args.initiative_messages and not args.initiative:
        raise ConfigurationError("--initiative-messages requires --initiative")
    if args.initiative_messages and not (args.console or args.sms_enabled):
        raise ConfigurationError(
            "--initiative-messages requires a configured operator delivery route "
            "(--console or enabled SMS)"
        )
    if args.initiative_continuation and not args.initiative:
        raise ConfigurationError("--initiative-continuation requires --initiative")
    if (args.initiative_continuation and
            not (args.initiative_actions or args.initiative_messages)):
        raise ConfigurationError(
            "--initiative-continuation requires --initiative-actions or "
            "--initiative-messages"
        )
    if args.initiative and args.cognition == "none":
        raise ConfigurationError("--initiative requires a cognition backend")
    if args.vision != "none" and args.camera == "none":
        raise ConfigurationError("--vision requires a camera backend")
    if args.vision != "none" and args.cognition == "none":
        raise ConfigurationError("--vision requires a cognition backend")
    if args.tts == "piper" and not args.piper_model:
        raise ConfigurationError("Piper TTS requires voice.piper_model or --piper-model")
    if args.tts == "elevenlabs" and not (
        args.elevenlabs_tts_voice_id and args.elevenlabs_tts_voice_id.strip()
    ):
        raise ConfigurationError(
            "ElevenLabs TTS requires voice.elevenlabs_tts_voice_id or "
            "--elevenlabs-tts-voice-id"
        )
    if args.tts == "espeak" and args.fallback_tts == "espeak":
        raise ConfigurationError("--fallback-tts espeak cannot equal primary espeak")
    if args.fusion_servo_test is not None and not args.diagnostics:
        raise ConfigurationError("--fusion-servo-test requires --diagnostics")
    if args.fusion_servo_test is not None and args.hardware != "fusion-hat":
        raise ConfigurationError("--fusion-servo-test requires --hardware fusion-hat")
    if args.fusion_battery_test and not args.diagnostics:
        raise ConfigurationError("--fusion-battery-test requires --diagnostics")
    if args.fusion_battery_test and args.hardware != "fusion-hat":
        raise ConfigurationError("--fusion-battery-test requires --hardware fusion-hat")
    if args.camera_test is not None and not args.diagnostics:
        raise ConfigurationError("--camera-test requires --diagnostics")
    if args.camera_test is not None and args.camera == "none":
        raise ConfigurationError("--camera-test requires a selected camera")
