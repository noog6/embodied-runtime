"""Command-line interface for the runtime."""

import argparse
import asyncio
import logging
from collections.abc import Sequence

from embodied_runtime.app import ApplicationOptions, RobotApplication, RuntimeSummary
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import ProfileLoadError, RobotProfile, load_profile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an embodied agent profile")
    parser.add_argument("startup_prompt", nargs="?", help="prompt for a future interaction system")
    parser.add_argument("--profile", default="mira", help="robot profile identifier")
    parser.add_argument("--hardware", choices=("virtual",), default="virtual")
    parser.add_argument("--diagnostics", action="store_true")
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


async def _run_application(args: argparse.Namespace, profile: RobotProfile) -> int:
    hardware = VirtualHardwareBackend()
    application = RobotApplication(
        profile, hardware, ApplicationOptions(startup_prompt=args.startup_prompt)
    )
    if args.diagnostics:
        try:
            await application.start()
            print(format_summary(application.summary()))
        finally:
            await application.stop()
        return 0

    await application.run()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        profile = load_profile(args.profile)
    except ProfileLoadError as error:
        build_parser().error(str(error))

    try:
        return asyncio.run(_run_application(args, profile))
    except KeyboardInterrupt:
        return 130
