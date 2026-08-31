# embodied-runtime

Reusable software runtime for embodied agents. Mira is the working name of the
initial reference/demo robot built with the runtime.

## Run the scaffold

Python 3.13 or later is required. From a fresh checkout, run:

```console
python main.py
python main.py "Good morning, Mira."
python main.py --diagnostics
python main.py --console
```

The local console is a read-only view of the running runtime's current state.
For example, enter `status`, `platform`, or `hardware` at the profile-derived
prompt, and enter `quit` to shut down cleanly. See [Local runtime
console](docs/console.md).

Diagnostics report both the selected robot hardware backend and a separate
snapshot of the real host platform, including available identity and resource
telemetry.

The runtime currently loads the Mira robot profile and uses the hardware-free
virtual backend by default. Physical SunFounder Fusion HAT+ support has not yet
been implemented. See [the architecture notes](docs/architecture.md) for the
runtime's intended boundaries.

Runtime log records use local ISO-8601 wall-clock timestamps with an explicit
timezone offset. A healthy run remains quiet between low-frequency heartbeats:

```text
2026-08-30T20:05:01.103-04:00 [APP] running profile=mira
2026-08-30T20:05:01.104-04:00 [PULSE] monitor=platform interval_s=5.0 heartbeat_s=60.0 status=ready
2026-08-30T20:06:01.205-04:00 [PULSE] heartbeat samples=12 cpu_temp_c=42.8 memory_available_mb=263 memory_available_pct=63.4 thermal=normal memory=normal
```

Monitor cadence uses monotonic time, independently of wall-clock timestamps.
The explicit `[DIAG]` and detailed `[PLATFORM]` report lines remain stable,
untimestamped structured snapshot output; surrounding runtime logs are timestamped.
