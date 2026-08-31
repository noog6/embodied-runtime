"""Command-line interface for the runtime."""

import argparse
import asyncio
from collections.abc import Sequence
import logging

from embodied_runtime.app import ApplicationOptions, RobotApplication, RuntimeSummary
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.console import AsyncLineTerminal, RuntimeConsole, run_console_session
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.logging_config import configure_logging
from embodied_runtime.profile import ProfileLoadError, RobotProfile, load_profile
from embodied_runtime.reflexes import PresenceCenteringReflex
from embodied_runtime.platform import PlatformSnapshot

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an embodied agent profile")
    parser.add_argument("startup_prompt", nargs="?", help="prompt for a future interaction system")
    parser.add_argument("--profile", default="mira", help="robot profile identifier")
    parser.add_argument("--hardware", choices=("virtual",), default="virtual")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--diagnostics", action="store_true")
    modes.add_argument("--console", action="store_true")
    return parser


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
    hardware = VirtualHardwareBackend()
    application = RobotApplication(
        profile, hardware, ApplicationOptions(startup_prompt=args.startup_prompt),
        body_backend=VirtualBodyBackend(),
        reflexes=(PresenceCenteringReflex(),),
    )
    if args.diagnostics:
        try:
            await application.start()
            application.refresh_platform_state()
            print(format_summary(application.summary()))
            assert application.runtime_state.platform is not None
            print(format_platform(application.runtime_state.platform))
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
    args = build_parser().parse_args(argv)
    configure_logging()
    try:
        profile = load_profile(args.profile)
    except ProfileLoadError as error:
        build_parser().error(str(error))

    try:
        return asyncio.run(_run_application(args, profile))
    except KeyboardInterrupt:
        return 130
