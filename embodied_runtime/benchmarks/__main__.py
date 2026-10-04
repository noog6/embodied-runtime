"""Command-line entry point for live cognition benchmark trials."""

import argparse
import asyncio
from pathlib import Path

from embodied_runtime.cognition.openai_responses import OpenAIResponsesBackend

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


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run cognition inside Mira's harness")
    parser.add_argument("--model", action="append", required=True,
                        help="OpenAI model string (repeatable)")
    parser.add_argument("--scenario", default=SCENARIO_ID, choices=tuple(SCENARIOS))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    return args


async def _main() -> int:
    args = _arguments()
    print(f"scenario: {args.scenario}", flush=True)
    report = await run_benchmark(
        lambda model: OpenAIResponsesBackend(model=model), args.model, args.repeat,
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
