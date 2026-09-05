"""Command-line interface for the runtime."""

import argparse
import asyncio
from collections.abc import Sequence
import logging
from pathlib import Path
import sys
import time

from embodied_runtime.app import ApplicationOptions, RobotApplication, RuntimeSummary
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
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.logging_config import configure_logging
from embodied_runtime.interaction import ConsoleOperatorMessageChannel
from embodied_runtime.profile import ProfileLoadError, RobotProfile, load_profile
from embodied_runtime.reflexes import PresenceCenteringReflex
from embodied_runtime.platform import PlatformMonitorPolicy, PlatformSnapshot
from embodied_runtime.perception import (
    OpenAIResponsesVisualPerceptionBackend, VisualPerceptionBackend,
)
from embodied_runtime.sensing.camera import CameraBackend
from embodied_runtime.sensing.camera.picamera2 import (
    Picamera2CameraBackend,
    Picamera2UnavailableError,
)

LOGGER = logging.getLogger(__name__)


def build_parser(*, explicit_configurable_values: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an embodied agent profile")
    parser.add_argument("startup_prompt", nargs="?", help="prompt for a future interaction system")
    def configurable_default(historical: object) -> object:
        return None if explicit_configurable_values else historical

    parser.add_argument("--config", type=Path, help="startup TOML configuration path")
    parser.add_argument("--profile", default=configurable_default("mira"),
                        help="robot profile identifier")
    parser.add_argument(
        "--hardware", choices=("virtual", "fusion-hat"),
        default=configurable_default("virtual")
    )
    parser.add_argument("--camera", choices=("none", "picamera2"),
                        default=configurable_default("none"))
    parser.add_argument("--no-color", action="store_true",
                        help="disable ANSI colour in console and runtime logs")
    parser.add_argument(
        "--cognition", choices=("none", "openai-responses"),
        default=configurable_default("none")
    )
    parser.add_argument(
        "--vision", choices=("none", "openai-responses"),
        default=configurable_default("none"),
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
    modes = parser.add_mutually_exclusive_group()
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
        help="read Fusion HAT battery ADC channel A4 once during diagnostics",
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
    args.console = effective.mode == "console"
    args.diagnostics = effective.mode == "diagnostics"
    args.initiative = effective.initiative
    args.initiative_platform_attention = effective.initiative_platform_attention
    args.initiative_actions = effective.initiative_actions
    args.initiative_messages = effective.initiative_messages
    args.initiative_continuation = effective.initiative_continuation
    args.initiative_goal_closure = effective.initiative_goal_closure
    return parser, args, effective


def _pwm_channel(value: str) -> str:
    try:
        return normalize_pwm_channel(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_hardware_backend(args: argparse.Namespace) -> HardwareBackend:
    if args.hardware == "fusion-hat":
        return FusionHatHardwareBackend()
    return VirtualHardwareBackend()


def build_camera_backend(args: argparse.Namespace) -> CameraBackend | None:
    if args.camera == "picamera2":
        return Picamera2CameraBackend()
    return None


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


def build_platform_monitor_policy(
    args: argparse.Namespace,
) -> PlatformMonitorPolicy | None:
    """Return the mode-specific monitor policy, preserving headless defaults."""
    if args.console:
        return PlatformMonitorPolicy(heartbeat_interval_seconds=None)
    return None


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
    """Read and format one Fusion HAT+ battery-divider measurement."""
    reading = hardware.read_battery_voltage()
    return (
        f"[BATTERY] adc_raw={reading.adc_raw} adc_v={reading.a4_voltage:.3f} "
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


async def _run_application(args: argparse.Namespace, profile: RobotProfile) -> int:
    hardware = build_hardware_backend(args)
    camera = build_camera_backend(args)
    cognition = build_cognition_backend(args)
    vision = build_visual_perception_backend(args)
    message_channel = ConsoleOperatorMessageChannel() if args.console else None
    application = RobotApplication(
        profile, hardware, ApplicationOptions(startup_prompt=args.startup_prompt,
                                              initiative_enabled=args.initiative,
                                              initiative_platform_attention_enabled=args.initiative_platform_attention,
                                              initiative_actions_enabled=args.initiative_actions,
                                              initiative_messages_enabled=args.initiative_messages,
                                              initiative_continuation_enabled=args.initiative_continuation,
                                              initiative_goal_closure_enabled=args.initiative_goal_closure),
        body_backend=VirtualBodyBackend(),
        reflexes=(PresenceCenteringReflex(),),
        camera_backend=camera,
        cognition_backend=cognition,
        visual_perception_backend=vision,
        operator_message_sink=message_channel,
        platform_monitor_policy=build_platform_monitor_policy(args),
    )
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
        try:
            await application.start()
            LOGGER.info("[CONSOLE] mode=local status=ready")
            await run_console_session(
                RuntimeConsole(application), AsyncLineTerminal(no_color=args.no_color),
                message_channel,
            )
        except asyncio.CancelledError:
            LOGGER.info("[APP] interrupted")
            raise
        finally:
            await application.stop()
        return 0

    await application.run()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser, args, _ = parse_launch_arguments(argv)
    if args.initiative_platform_attention and not args.initiative:
        parser.error("--initiative-platform-attention requires --initiative")
    if args.initiative_goal_closure and not args.initiative:
        parser.error("--initiative-goal-closure requires --initiative")
    if args.initiative_actions and not args.initiative:
        parser.error("--initiative-actions requires --initiative")
    if args.initiative_messages and not args.initiative:
        parser.error("--initiative-messages requires --initiative")
    if args.initiative_messages and not args.console:
        parser.error("--initiative-messages requires --console")
    if args.initiative_continuation and not args.initiative:
        parser.error("--initiative-continuation requires --initiative")
    if (args.initiative_continuation and
            not (args.initiative_actions or args.initiative_messages)):
        parser.error(
            "--initiative-continuation requires --initiative-actions or "
            "--initiative-messages"
        )
    if args.initiative and args.cognition == "none":
        parser.error("--initiative requires a cognition backend")
    if args.vision != "none" and args.camera == "none":
        parser.error("--vision requires a camera backend")
    if args.vision != "none" and args.cognition == "none":
        parser.error("--vision requires a cognition backend")
    if args.fusion_servo_test is not None and not args.diagnostics:
        parser.error("--fusion-servo-test requires --diagnostics")
    if args.fusion_servo_test is not None and args.hardware != "fusion-hat":
        parser.error("--fusion-servo-test requires --hardware fusion-hat")
    if args.fusion_battery_test and not args.diagnostics:
        parser.error("--fusion-battery-test requires --diagnostics")
    if args.fusion_battery_test and args.hardware != "fusion-hat":
        parser.error("--fusion-battery-test requires --hardware fusion-hat")
    if args.camera_test is not None and not args.diagnostics:
        parser.error("--camera-test requires --diagnostics")
    if args.camera_test is not None and args.camera == "none":
        parser.error("--camera-test requires a selected camera")
    configure_logging(no_color=args.no_color)
    if args.config is not None:
        LOGGER.info("[CONFIG] source=%s status=loaded", args.config)
    try:
        profile = load_profile(args.profile)
    except ProfileLoadError as error:
        parser.error(str(error))

    try:
        return asyncio.run(_run_application(args, profile))
    except (FusionHatUnavailableError, Picamera2UnavailableError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
