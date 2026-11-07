"""Attach a terminal to an already running local agent; never launch a runtime."""

from embodied_runtime.console_client import main


if __name__ == "__main__":
    raise SystemExit(main())
