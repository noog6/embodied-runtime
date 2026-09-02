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

Optional provider setup is documented in [OpenAI text cognition
setup](docs/cognition-openai.md), including the linked [Secrets and API
keys](docs/secrets.md) procedure for local credential storage and delivery.

The local console projects the running runtime's current state and provides
two deliberately narrow development controls. For example, enter `status`,
`body`, or `presence` at the profile-derived
prompt, and enter `quit` to shut down cleanly. See [Local runtime
console](docs/console.md).

Diagnostics report both the selected robot hardware backend and a separate
snapshot of the real host platform, including available identity and resource
telemetry.

The runtime loads the Mira robot profile and uses hardware-free virtual hardware
and body backends by default. An explicitly selected SunFounder Fusion HAT+
backend provides board readiness and low-level PWM access; the semantic body
intentionally remains virtual. See [the hardware notes](docs/hardware/fusion-hat-plus.md)
and [architecture notes](docs/architecture.md).

The default demo also includes one deterministic local reflex: a transition to
semantic presence centers body yaw and pitch through the application body API.
It is source-independent, owns no state, and involves no AI or cognition. See
[Local reflexes](docs/reflexes.md).

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
