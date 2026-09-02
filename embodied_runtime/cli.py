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
from embodied_runtime.profile import ProfileLoadError, RobotProfile, load_profile
from embodied_runtime.reflexes import PresenceCenteringReflex
from embodied_runtime.platform import PlatformSnapshot
from embodied_runtime.sensing.camera import CameraBackend
from embodied_runtime.sensing.camera.picamera2 import (
    Picamera2CameraBackend,
    Picamera2UnavailableError,
)

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an embodied agent profile")
    parser.add_argument("startup_prompt", nargs="?", help="prompt for a future interaction system")
    parser.add_argument("--profile", default="mira", help="robot profile identifier")
    parser.add_argument(
        "--hardware", choices=("virtual", "fusion-hat"), default="virtual"
    )
    parser.add_argument("--camera", choices=("none", "picamera2"), default="none")
    parser.add_argument(
        "--cognition", choices=("none", "openai-responses"), default="none"
    )
    parser.add_argument("--initiative", action="store_true",
                        help="enable goal-directed read-only cognition initiative")
    parser.add_argument(
        "--initiative-actions", action="store_true",
        help="allow initiative one bounded nonphysical semantic body action",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--diagnostics", action="store_true")
    modes.add_argument("--console", action="store_true")
    parser.add_argument(
        "--fusion-servo-test",
        metavar="P0..P11",
        type=_pwm_channel,
        help="CAUTION: explicitly actuate one Fusion HAT bench-servo PWM channel",
    )
    parser.add_argument(
        "--camera-test",
        metavar="OUTPUT_PATH",
        type=Path,
        help="capture exactly one JPEG to this path during diagnostics",
    )
    return parser


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
    application = RobotApplication(
        profile, hardware, ApplicationOptions(startup_prompt=args.startup_prompt,
                                              initiative_enabled=args.initiative,
                                              initiative_actions_enabled=args.initiative_actions),
        body_backend=VirtualBodyBackend(),
        reflexes=(PresenceCenteringReflex(),),
        camera_backend=camera,
        cognition_backend=cognition,
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
            await run_console_session(RuntimeConsole(application), AsyncLineTerminal())
        except asyncio.CancelledError:
            LOGGER.info("[APP] interrupted")
            raise
        finally:
            await application.stop()
        return 0

    await application.run()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.initiative_actions and not args.initiative:
        parser.error("--initiative-actions requires --initiative")
    if args.initiative and args.cognition == "none":
        parser.error("--initiative requires a cognition backend")
    if args.fusion_servo_test is not None and not args.diagnostics:
        parser.error("--fusion-servo-test requires --diagnostics")
    if args.fusion_servo_test is not None and args.hardware != "fusion-hat":
        parser.error("--fusion-servo-test requires --hardware fusion-hat")
    if args.camera_test is not None and not args.diagnostics:
        parser.error("--camera-test requires --diagnostics")
    if args.camera_test is not None and args.camera == "none":
        parser.error("--camera-test requires a selected camera")
    configure_logging()
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
