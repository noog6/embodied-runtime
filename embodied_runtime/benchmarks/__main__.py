"""Command-line entry point for live cognition benchmark trials."""

import argparse
import asyncio
from pathlib import Path

from embodied_runtime.cognition.factory import (
    add_compatible_arguments, create_cognition_backend, validate_cognition_configuration,
)
from embodied_runtime.config import ConfigurationError, load_runtime_config, resolve_launch_configuration

from .runner import render_report, run_benchmark
from .scenario import SCENARIOS, SCENARIO_ID


def _progress(model, repetition, repeat, result) -> None:
    if result is None:
        print(f"model: {model}\ntrial {repetition}/{repeat} running...", flush=True)
    else:
        outcome = "PASS" if result.passed else "FAIL"
        print(
            f"trial {repetition}/{repeat} {outcome} "
            f"{result.wall_duration_seconds:.1f}s",
            flush=True,
        )


def _arguments(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run cognition inside Mira's harness")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--backend", dest="cognition", choices=("openai-responses", "openai-compatible"))
    parser.add_argument("--model", action="append", help="explicit model string (repeatable)")
    add_compatible_arguments(parser)
    parser.add_argument("--scenario", default=SCENARIO_ID, choices=tuple(SCENARIOS))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    try:
        configured = load_runtime_config(args.config) if args.config is not None else None
        if args.cognition is None and (configured is None or configured.runtime.cognition is None):
            args.cognition = "openai-responses"
        effective = resolve_launch_configuration(args, configured)
        args.cognition = effective.cognition
        args.cognition_openai_compatible = effective.cognition_openai_compatible
        configured_model = None
        if configured is not None:
            configured_model = (configured.cognition.openai_compatible.model
                                if args.cognition == "openai-compatible" else configured.cognition.model)
        args.model = args.model or ([configured_model] if configured_model else [])
        if not args.model:
            raise ConfigurationError("benchmark requires --model or a configured cognition model")
        if args.cognition == "none":
            raise ConfigurationError("benchmark requires a cognition backend")
        for model in args.model:
            validate_cognition_configuration(args.cognition, model, args.cognition_openai_compatible)
    except ConfigurationError as error:
        parser.error(str(error))
    return args


def build_benchmark_backend(args, model, *, client=None):
    return create_cognition_backend(
        args.cognition, model=model, compatible=args.cognition_openai_compatible,
        client=client,
    )


async def _main() -> int:
    args = _arguments()
    print(f"scenario: {args.scenario}", flush=True)
    report = await run_benchmark(
        lambda model: build_benchmark_backend(args, model), args.model, args.repeat,
        scenario_id=args.scenario, progress=_progress,
    )
    print(render_report(report), end="")
    if args.json_out is not None:
        args.json_out.write_text(report.to_json(), encoding="utf-8")
        print(f"JSON written to {args.json_out}")
    return 0 if all(trial.passed for trial in report.trials) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
